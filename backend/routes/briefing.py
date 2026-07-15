"""
晨间星闻路由 — 科技动态(AI HOT API) + 行业/政策动态(自建抓取)
GET  /api/briefing           → 科技动态精选
GET  /api/briefing/logistics → 跨境行业与政策动态
POST /api/briefing/refresh   → 强制刷新全部缓存
"""
import asyncio
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from featured import (
    auto_format_featured,
    build_featured_candidates,
    finalize_featured,
    get_featured_response,
    is_manual_featured,
    load_featured_store,
)
from ops_format import auto_format_ops_items
from settings import allow_page_stale_refresh, get_data_dir, require_auth, resolve_query_date, today_local

router = APIRouter(prefix="/api/briefing", tags=["晨间星闻"])

# 缓存策略
_cache_ai: dict | None = None
_cache_logistics: dict | None = None
_cache_time_ai: float = 0
_cache_time_logistics: float = 0
_CACHE_TTL = 300  # 5 分钟内存缓存，超时自动拉取最新
_CACHE_FILE_KEEP_DAYS = 7  # 磁盘日期快照保留 7 天，更早的自动清理
_logistics_refresh_task: asyncio.Task | None = None
_featured_auto_task: asyncio.Task | None = None
_featured_auto_started_at: float = 0
_FEATURED_AUTO_COOLDOWN_SECONDS = 90
_logistics_refresh_started_at: float = 0
_last_logistics_refresh: dict = {
    "ok": None,
    "started_at": "",
    "finished_at": "",
    "duration_sec": None,
    "reason": "",
    "error": "",
    "items": 0,
    "counts_by_task": {},
    "fallback_added": 0,
}
_policy_translation_cache_lock = threading.Lock()
try:
    _LOGISTICS_REFRESH_TOTAL_TIMEOUT = max(60, int(os.getenv("LOGISTICS_REFRESH_TOTAL_TIMEOUT", "600")))
except ValueError:
    _LOGISTICS_REFRESH_TOTAL_TIMEOUT = 600
try:
    _LOGISTICS_REFRESH_STALE_SECONDS = max(120, int(os.getenv("LOGISTICS_REFRESH_STALE_SECONDS", "900")))
except ValueError:
    _LOGISTICS_REFRESH_STALE_SECONDS = 900

# 并发锁：防止多人同时访问时重复触发抓取（thundering herd）
_lock_ai = asyncio.Lock()
_lock_logistics = asyncio.Lock()

API_BASE = "https://aihot.virxact.com"
_AIHOT_HEADERS = {
    "User-Agent": "YunXiaoXing-Daily/1.0 (logistics-tool; +https://github.com)",
    "Accept": "application/json",
}

# 行业/政策相关任务类型：前端再按 task + 关键词归入行业动态或政策动态。
LOGISTICS_TASKS = [
    ("logistics-daily",      "行业动态｜物流资讯"),
    ("crossborder-platform", "行业动态｜跨境平台"),
    ("shipping-port",        "行业动态｜航运港口"),
    ("by56-wiki",            "行业动态｜百运百科"),
    ("global-news",          "政策动态｜地缘与国际风险"),
    ("policy-official",      "政策动态｜关税与官方政策"),
]
TASK_LABELS = dict(LOGISTICS_TASKS)
ROOT = Path(__file__).resolve().parent.parent.parent
POLICY_DISPLAY_TASKS = {"policy-official", "global-news"}
INDUSTRY_DISPLAY_TRANSLATION_TASKS = {"shipping-port", "logistics-daily"}
INDUSTRY_DISPLAY_TRANSLATION_SOURCES = {
    "aircargoweek",
    "stattimes_aircargo",
    "freightwaves",
    "theloadstar",
}
POLICY_DISPLAY_TRANSLATION_CACHE = "policy_display_translation_cache.json"
AI_CACHE_FILE = "ai_cache.json"
POLICY_DISPLAY_TRANSLATION_LIMIT = int(os.getenv("POLICY_DISPLAY_TRANSLATION_LIMIT", "15"))
try:
    MIN_LOGISTICS_ITEMS_PER_TASK = max(0, int(os.getenv("MIN_LOGISTICS_ITEMS_PER_TASK", "4")))
except ValueError:
    MIN_LOGISTICS_ITEMS_PER_TASK = 4
try:
    MAX_LOGISTICS_FALLBACK_PER_TASK = max(0, int(os.getenv("MAX_LOGISTICS_FALLBACK_PER_TASK", "3")))
except ValueError:
    MAX_LOGISTICS_FALLBACK_PER_TASK = 3
try:
    MAX_LOGISTICS_FALLBACK_TOTAL = max(0, int(os.getenv("MAX_LOGISTICS_FALLBACK_TOTAL", "8")))
except ValueError:
    MAX_LOGISTICS_FALLBACK_TOTAL = 8
try:
    _LOGISTICS_STALE_TRIGGER_SECONDS = max(600, int(os.getenv("LOGISTICS_STALE_TRIGGER_SECONDS", "14400")))
except ValueError:
    _LOGISTICS_STALE_TRIGGER_SECONDS = 14400
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_RECRUITMENT_RE = re.compile(
    r"招聘|求职|职位|岗位|诚聘|急招|招募|人才招聘|简历|投递|薪资|"
    r"工作机会|热门职位|加入我们|社招|校招|内推|"
    r"\b(?:hiring|job|jobs|career|careers|recruit|recruitment|resume|cv)\b",
    re.I,
)
_ARTICLE_META_PREFIX_RE = re.compile(
    r"^(?:[\u4e00-\u9fffA-Za-z0-9&＋+_.·・\-]{2,30}\s*[•·]\s*)?"
    r"(?:刚刚|\d+\s*(?:秒|分钟|小时|天|周|月)前|20\d{2}[年/-]\d{1,2}[月/-]\d{1,2}(?:日)?)\s*"
    r"(?:[•·]\s*(?!(?:阅读|浏览|点击)\b)[^•·]{1,20}){0,2}\s*"
    r"(?:[•·]\s*(?:阅读|浏览|点击)\s*\d+)?\s*",
    re.I,
)
_POLICY_DISPLAY_RE = re.compile(
    r"关税|税率|Section\s*301|Section\s*232|反倾销|反补贴|贸易救济|贸易摩擦|"
    r"tariff|duties|anti-dumping|countervailing|customs duty|de minimis|"
    r"HS编码|HS code|原产地|海关公告|清关政策|CBP|USTR|Federal Register|"
    r"OFAC|BIS|SDN|Entity List|出口管制|制裁|禁运|限制名单|地缘|红海|海峡|"
    r"封锁|战争|冲突|空域关闭|港口封锁|罢工|WTO|FTA|商务部|财政部|税则",
    re.I,
)
_POLICY_SUBSTANTIVE_RE = re.compile(
    r"公告|发布|生效|实施|调整|新增|修订|延长|暂停|恢复|提高|降低|取消|豁免|"
    r"征收|加征|反倾销|反补贴|调查|裁定|制裁|禁运|出口管制|实体清单|"
    r"关税|税率|清关|申报|查验|许可证|监管条件|原产地|HS编码|"
    r"tariff|duty|duties|effective|takes effect|announced|notice|final rule|"
    r"proposed rule|amend|revise|increase|decrease|suspend|extend|exempt|"
    r"sanction|export control|entity list|antidumping|countervailing|customs|"
    r"clearance|license|licence|classification|origin",
    re.I,
)
_LOW_VALUE_POLICY_RE = re.compile(
    r"高级文档搜索|文档搜索|Document Search|Advanced Document Search|"
    r"sign up for email updates|email updates|subscribe|newsletter|"
    r"skip to main content|view table of contents|export as pdf|"
    r"mission the office|office of .* affairs develops and implements|"
    r"responsible for developing and implementing|"
    r"about us|contact us|webinars?|events?|press releases?|speeches?|"
    r"site map|search results|login|register|download app|"
    r"仅为标题|无具体政策|无直接影响|不涉及清关政策|系统优化|"
    r"物流影响需人工复核|"
    r"javascript is not enabled|sanctions list service application|"
    r"cannot run the sanctions list service|"
    r"sanctions list search|consolidated sanctions list|non-sdn lists|"
    r"sign up for .* sanctions",
    re.I,
)
_HARD_LOW_VALUE_POLICY_RE = re.compile(
    r"无具体政策|无直接影响|不涉及清关政策|不涉及.*法规.*实质性变更|"
    r"系统优化|仅为标题|物流影响需人工复核|高级文档搜索|文档搜索|"
    r"Advanced Document Search|Document Search|sign up for email updates|"
    r"skip to main content|view table of contents|export as pdf",
    re.I,
)
_LOW_VALUE_POLICY_TITLES_RE = re.compile(
    r"^(?:Unverified List|Denied Persons List|Entity List|SDN List|"
    r"Consolidated Screening List|Western Hemisphere|Japan, Korea & APEC|"
    r"South & Central Asia|Trade\.gov Consolidated Screening List|"
    r"Consolidated Sanctions List(?: \(Non-SDN Lists\))?|"
    r"Sanctions List Search|Sanctions List Service(?: \(SLS\))?|"
    r"Iran Sanctions)$",
    re.I,
)


def _clean_feed_title(title: str) -> str:
    """把抓取标题压成类似 AI HOT 的短标题。"""
    title = " ".join((title or "").split())
    title = re.sub(r"^(?:\d+\s*(?:秒|分钟|小时|天|周|月)前|刚刚)\s*", "", title)
    title = re.sub(
        r"^(?:海运新闻|空运新闻|世界海关|跨境电商|物流资讯|国际物流|港航新闻|快递快运|航运新闻)\s+",
        "",
        title,
    )
    title = re.sub(r"\s*分享至\s*$", "", title)
    return title.strip()


def _policy_translation_cache_path() -> Path:
    return get_data_dir() / POLICY_DISPLAY_TRANSLATION_CACHE


def _ai_cache_path() -> Path:
    return get_data_dir() / AI_CACHE_FILE


def _is_mostly_english(text: str, *, min_latin: int = 40) -> bool:
    if not text:
        return False
    latin_count = len(_LATIN_RE.findall(text))
    cjk_count = len(_CJK_RE.findall(text))
    return latin_count >= min_latin and latin_count > max(cjk_count * 3, 20)


def _is_policy_display_item(item: dict) -> bool:
    task = item.get("task") or item.get("_task") or item.get("task_type") or ""
    tags = " ".join(str(t) for t in (item.get("tags") or item.get("ai_tags") or []))
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("title", "summary", "ai_summary", "analysis", "ai_analysis", "source_name", "label", "_task_label")
    )
    haystack = f"{haystack} {tags}"
    return task in POLICY_DISPLAY_TASKS or bool(_POLICY_DISPLAY_RE.search(haystack))


def _is_recruitment_item(title: str, summary: str, source_url: str = "") -> bool:
    return bool(_RECRUITMENT_RE.search(" ".join([title or "", summary or "", source_url or ""])))


def _clean_common_summary(summary: str, title: str = "") -> str:
    """清理历史缓存/数据库里混入的站点模板、客服、公众号等噪音。"""
    from routes.scrape import strip_reading_meta

    text = re.sub(r"^\ufeff+", "", summary or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = strip_reading_meta(text)
    text = _ARTICLE_META_PREFIX_RE.sub("", text).strip()
    if title:
        text = re.sub(rf"^{re.escape(title)}(?:\s*_跨境知道)?\s*", "", text)

    noise_patterns = [
        r"客服\s*跨境知道网客服.*?(?:返回顶部|$)",
        r"加我微信.*?(?:返回顶部|$)",
        r"有小雨，跨境出海不迷路",
        r"公众号\s*跨境知道网公众号.*?(?:返回顶部|$)",
        r"微信扫一扫关注.*?(?:返回顶部|$)",
        r"及时了解最新跨境前沿资讯.*?(?:返回顶部|$)",
        r"文章经授权转载自公众号[:：]\s*[^ ]+\s*",
        r"客服电话[:：]?\s*[\d\-+() ]+.*?(?:©|$)",
        r"邮箱[:：]?\s*[\w.+-]+@[\w.-]+.*?(?:©|$)",
        r"WIFFA公众号.*?(?:©|$)",
        r"舱哪儿云公众号.*?(?:©|$)",
        r"国际海运网\s*©.*$",
        r"^(?:当前位置[:：]\s*)?首页\s*>\s*[^ ]+\s*",
        r"^首页\s*>\s*新闻发布(?:\s*>\s*[^ ]+)?\s*来源[:：][^ ]+\s*类型[:：][^ ]+\s*分类[:：][^ ]+\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*",
        r"本网站标明来源的其他媒体信息.*$",
        r"返回顶部",
        r"上一篇[:：]?.*",
        r"下一篇[:：]?.*",
        r"相关阅读.*",
        r"相关推荐.*",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, " ", text, flags=re.I)

    text = _ARTICLE_META_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"\s{2,}", " ", text).strip(" _-｜|")
    return text


def _clean_by56_summary(summary: str, title: str = "") -> str:
    text = re.sub(r"\s+", " ", summary or "").strip()
    if title:
        text = re.sub(rf"^{re.escape(title)}\s*", "", text)
    noise_patterns = [
        r"20\d{2}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{1,2}:\d{1,2}\s+更新",
        r"\d+\s*浏览",
        r"作者[:：]\s*百运网",
        r"货物所在地\s*目的国家\s*货物信息\s*KG\s*获取报价",
        r"获取报价",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, " ", text, flags=re.I)
    text = re.sub(r"\s{2,}", " ", text).strip()
    sentences = re.split(r"(?<=[。！？；])\s*", text)
    picked = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 16:
            continue
        picked.append(sentence)
        if len("".join(picked)) >= 120 or len(picked) >= 3:
            break
    text = "".join(picked) if picked else text
    if len(text) > 180:
        cut_pos = text[:180].rfind("。")
        if cut_pos < 60:
            cut_pos = text[:180].rfind("，")
        if cut_pos < 60:
            cut_pos = 178
        text = text[:cut_pos + 1].rstrip("，。； ") + "…"
    return text


def _build_by56_analysis(title: str, summary: str, body_text: str = "") -> str:
    text = _clean_by56_summary(body_text or summary, title)
    if not text:
        return ""
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[。！？；])\s*", text)
        if len(s.strip()) >= 12
    ]
    core = sentences[0] if sentences else text[:100]
    reminder = ""
    for sentence in sentences[1:]:
        if re.search(r"罚款|延误|扣货|查验|成本|费用|申报|时效|风险|承担|计算|查询|操作|注意", sentence):
            reminder = sentence
            break
    if not reminder and len(sentences) > 1:
        reminder = sentences[1]
    parts = [f"核心解释：{core}"]
    if reminder:
        parts.append(f"实操提醒：{reminder}")
    return "\n".join(parts)


def _has_stale_policy_year(text: str) -> bool:
    years = [int(year) for year in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text or "")]
    if not years:
        return False
    current_year = datetime.now().astimezone().year
    allowed_years = {current_year, current_year - 1}
    stale_years = [year for year in years if year not in allowed_years]
    if not stale_years:
        return False

    # 如果标题/摘要明确说旧法规被当前公告修订、废止或更新，则保留给人工看。
    has_current_year = bool(re.search(rf"(?<!\d){current_year}(?!\d)|{current_year}年", text))
    has_revision_action = bool(re.search(
        r"最新|新版|更新|修订|废止|替代|延长|amend|revise|update|replace|supersede|extend",
        text,
        re.I,
    ))
    return not (has_current_year and has_revision_action)


def _is_low_value_policy_item(item: dict) -> bool:
    if not _is_policy_display_item(item):
        return False
    title = str(item.get("title") or item.get("original_title") or "").strip()
    summary = str(item.get("summary") or item.get("ai_summary") or item.get("analysis") or "").strip()
    source_name = str(item.get("source_name") or item.get("source") or "").strip()
    source_url = str(item.get("source_url") or item.get("url") or "").strip()
    text = " ".join([title, summary, source_name, source_url])

    if _has_stale_policy_year(text):
        return True
    if _HARD_LOW_VALUE_POLICY_RE.search(text):
        return True
    if _LOW_VALUE_POLICY_TITLES_RE.fullmatch(title):
        return True
    if _LOW_VALUE_POLICY_RE.search(text) and not _POLICY_SUBSTANTIVE_RE.search(text):
        return True

    mostly_english = _is_mostly_english(text)
    if mostly_english and _LOW_VALUE_POLICY_RE.search(text):
        return True

    # 官方站的常驻清单、栏目介绍、搜索页如果没有新日期/新动作，不作为晨间重点推送。
    has_date = bool(re.search(r"\b20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}|\b20\d{2}\b", text))
    if mostly_english and not has_date and not _POLICY_SUBSTANTIVE_RE.search(text):
        return True
    return False


def _needs_policy_display_translation(item: dict) -> bool:
    if not _is_policy_display_item(item) or _is_low_value_policy_item(item):
        return False
    text = " ".join(
        str(item.get(key) or "")
        for key in ("title", "summary", "ai_summary", "analysis", "ai_analysis", "body_text")
    )
    return _is_mostly_english(text, min_latin=30)


def _needs_industry_display_translation(item: dict) -> bool:
    task = item.get("task") or item.get("_task") or item.get("task_type") or ""
    source_id = item.get("source_id") or ""
    source_name = str(item.get("source_name") or "").lower()
    source_url = str(item.get("source_url") or item.get("url") or "").lower()
    if task not in INDUSTRY_DISPLAY_TRANSLATION_TASKS:
        return False
    source_hit = (
        source_id in INDUSTRY_DISPLAY_TRANSLATION_SOURCES
        or "air cargo week" in source_name
        or "stat times" in source_name
        or "aircargoweek.com" in source_url
        or "stattimes.com" in source_url
        or "freightwaves.com" in source_url
        or "theloadstar.com" in source_url
    )
    if not source_hit:
        return False
    text = " ".join(
        str(item.get(key) or "")
        for key in ("title", "summary", "ai_summary", "analysis", "ai_analysis", "body_text")
    )
    return _is_mostly_english(text)


def _policy_translation_cache_key(item: dict) -> str:
    source_url = item.get("source_url") or item.get("url") or ""
    title = item.get("original_title") or item.get("title") or ""
    summary = item.get("original_summary") or item.get("summary") or item.get("ai_summary") or ""
    raw = f"{source_url}\n{title}\n{summary}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _load_policy_translation_cache() -> dict:
    path = _policy_translation_cache_path()
    if not path.exists():
        return {}
    try:
        with _policy_translation_cache_lock:
            with open(path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        return cache if isinstance(cache, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_policy_translation_cache(cache: dict) -> None:
    path = _policy_translation_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _policy_translation_cache_lock:
        existing = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update(cache)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        tmp.replace(path)


def _normalize_aihot_payload(data: dict) -> dict:
    """兼容 AI HOT items/daily/sections/data 等不同返回结构。"""
    if not isinstance(data, dict):
        return {"items": []}

    payload = dict(data)
    items = payload.get("items") or []
    if not items and isinstance(payload.get("sections"), list):
        items = []
        for section in payload["sections"]:
            if isinstance(section, dict):
                items.extend(section.get("items") or [])
        payload["items"] = items

    if not items and isinstance(payload.get("data"), dict):
        nested = _normalize_aihot_payload(payload["data"])
        if nested.get("items"):
            payload.update(nested)

    payload["items"] = payload.get("items") or []
    payload["total"] = payload.get("total") or payload.get("count") or len(payload["items"])
    return payload


def _load_ai_cache() -> dict | None:
    path = _ai_cache_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    normalized = _normalize_aihot_payload(payload)
    return normalized if normalized.get("items") else None


def _save_ai_cache(payload: dict) -> None:
    normalized = _normalize_aihot_payload(payload)
    if not normalized.get("items"):
        return
    normalized["cached_at"] = datetime.now(timezone.utc).isoformat()
    path = _ai_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _strip_json_fence(content: str) -> str:
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
    first = content.find("{")
    last = content.rfind("}")
    if first >= 0 and last > first:
        return content[first:last + 1]
    return content


def _translate_display_item(item: dict, client, llm_config: dict, mode: str = "policy") -> dict | None:
    title = str(item.get("original_title") or item.get("title") or "").strip()
    summary = str(item.get("original_summary") or item.get("summary") or item.get("ai_summary") or "").strip()
    body_text = str(item.get("body_text") or item.get("content_snippet") or "").strip()
    source_name = str(item.get("source_name") or item.get("source") or "").strip()
    source_url = str(item.get("source_url") or item.get("url") or "").strip()
    body_excerpt = body_text[:1200] if body_text and _is_mostly_english(body_text) else ""

    if mode == "industry":
        system_content = "你是百运网“晨间星闻”的跨境物流行业动态中文编辑，只做忠实翻译和业务化摘要。"
        user_prompt = (
            "请把下面这条英文空运/物流行业新闻翻译并改写成简体中文展示文案，返回严格 JSON，"
            "只包含 title 和 summary 两个字段。\n\n"
            "要求：\n"
            "- title：18-36个中文字符，直接体现空运/物流市场、运力、货量、航司、机场、运价或时效影响。\n"
            "- summary：简明扼要地说明对跨境物流业务的可能影响，篇幅根据内容复杂度自行判断；没有明确业务影响时只做客观摘要。\n"
            "- 保留航空公司、机场、国家/地区、指数、机构名等关键名词。\n"
            "- 不得编造原文没有的数字、日期、运价变化或结论。\n"
            "- 不要营销化，不要输出招聘、活动报名、广告推广内容。\n\n"
            f"来源：{source_name}\n"
            f"链接：{source_url}\n"
            f"原标题：{title}\n"
            f"原摘要：{summary}\n"
            f"原文补充：{body_excerpt}"
        )
    else:
        system_content = "你是百运网“晨间星闻”的政策动态中文展示翻译助手，只做忠实翻译和业务化改写。"
        user_prompt = (
            "请把下面这条英文政策/地缘风险信息翻译并改写成简体中文展示文案，返回严格 JSON，"
            "只包含 title 和 summary 两个字段。\n\n"
            "要求：\n"
            "- title：20-38个中文字符，保留 USTR、CBP、OFAC、BIS、Section 301、Entity List 等机构名、法规编号或清单名称。\n"
            "- summary：简明扼要地说明事实和可能影响，用跨境物流销售、客服、操作能理解的话表达，篇幅自行判断。\n"
            "- 不得编造原文没有的日期、税率、HS编码、金额、生效时间或政策结论。\n"
            "- 如果原文只是机构/清单说明，未给出明确新政策或物流影响，请写明“物流影响需人工复核”。\n"
            "- 保持中性、准确，不要营销化。\n\n"
            f"来源：{source_name}\n"
            f"链接：{source_url}\n"
            f"原标题：{title}\n"
            f"原摘要：{summary}\n"
            f"原文补充：{body_excerpt}"
        )

    try:
        response = client.chat.completions.create(
            model=llm_config.get("model", "deepseek-chat"),
            messages=[
                {
                    "role": "system",
                    "content": system_content,
                },
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=min(int(llm_config.get("max_tokens", 2048)), 900),
        )
        content = _strip_json_fence(response.choices[0].message.content)
        result = json.loads(content)
    except Exception:
        return None

    if not isinstance(result, dict):
        return None
    translated_title = str(result.get("title") or "").strip()
    translated_summary = str(result.get("summary") or "").strip()
    if len(_CJK_RE.findall(f"{translated_title}{translated_summary}")) < 20:
        return None
    if not translated_title or not translated_summary:
        return None
    return {
        "title": translated_title[:80],
        "summary": translated_summary[:240],
    }


def _get_policy_display_llm_client():
    """优先给后续专门翻译服务预留环境变量；当前兼容 OpenAI/DeepSeek 格式。"""
    try:
        from openai import OpenAI  # noqa: E402
        from routes.llm import get_llm_client  # noqa: E402
        from settings import load_llm_config  # noqa: E402
    except Exception:
        return None, {}

    llm_config = load_llm_config()
    translate_key = os.getenv("TRANSLATE_API_KEY", "").strip()
    if translate_key:
        translate_config = {
            **llm_config,
            "api_key": translate_key,
            "base_url": os.getenv("TRANSLATE_BASE_URL", llm_config.get("base_url", "https://api.deepseek.com")),
            "model": os.getenv("TRANSLATE_MODEL", llm_config.get("model", "deepseek-chat")),
            "max_tokens": int(os.getenv("TRANSLATE_MAX_TOKENS", str(llm_config.get("max_tokens", 2048)))),
        }
        client = OpenAI(
            api_key=translate_key,
            base_url=translate_config["base_url"],
        )
        return client, translate_config

    return get_llm_client(), llm_config


def _merge_policy_display_translation(item: dict, translated: dict, source: str) -> dict:
    original_title = item.get("original_title") or item.get("title") or ""
    original_summary = item.get("original_summary") or item.get("summary") or item.get("ai_summary") or ""
    return {
        **item,
        "original_title": original_title,
        "original_summary": original_summary,
        "title": translated.get("title") or item.get("title") or "",
        "summary": translated.get("summary") or item.get("summary") or "",
        "ai_summary": translated.get("summary") or item.get("ai_summary") or item.get("summary") or "",
        "_display_translated": True,
        "_display_translation_source": source,
    }


def _policy_display_items(items: list[dict]) -> tuple[list[dict], int]:
    filtered = []
    hidden = 0
    for item in items:
        if isinstance(item, dict) and _is_low_value_policy_item(item):
            hidden += 1
            continue
        filtered.append(item)
    return filtered, hidden


def _apply_policy_display_translations_sync(response: dict) -> dict:
    items = response.get("items") or []
    if not items:
        return response
    items, hidden_low_value = _policy_display_items(items)

    cache = _load_policy_translation_cache()
    new_cache: dict = {}
    translated_count = 0
    cached_count = 0
    llm_calls = 0
    llm_unavailable = False
    llm_config = None
    client = None
    out_items = []

    for item in items:
        if not isinstance(item, dict):
            out_items.append(item)
            continue
        needs_policy = _needs_policy_display_translation(item)
        needs_industry = _needs_industry_display_translation(item)
        if not needs_policy and not needs_industry:
            out_items.append(item)
            continue

        cache_key = _policy_translation_cache_key(item)
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("title") and cached.get("summary"):
            out_items.append(_merge_policy_display_translation(item, cached, "cache"))
            cached_count += 1
            continue

        if llm_unavailable or llm_calls >= POLICY_DISPLAY_TRANSLATION_LIMIT:
            out_items.append(item)
            continue

        if client is None:
            try:
                client, llm_config = _get_policy_display_llm_client()
            except Exception:
                client = None
            if client is None:
                llm_unavailable = True
                out_items.append(item)
                continue

        translated = _translate_display_item(
            item,
            client,
            llm_config or {},
            mode="policy" if needs_policy else "industry",
        )
        llm_calls += 1
        if translated:
            cache_value = {
                **translated,
                "source_url": item.get("source_url") or item.get("url") or "",
                "translated_at": datetime.now(timezone.utc).isoformat(),
            }
            new_cache[cache_key] = cache_value
            out_items.append(_merge_policy_display_translation(item, cache_value, "llm"))
            translated_count += 1
        else:
            out_items.append(item)

    if new_cache:
        _save_policy_translation_cache(new_cache)

    if translated_count or cached_count:
        return {
            **response,
            "items": out_items,
            "total": len(out_items),
            "policy_display_translation": {
                "translated": translated_count,
                "from_cache": cached_count,
                "limit": POLICY_DISPLAY_TRANSLATION_LIMIT,
                "hidden_low_value": hidden_low_value,
            },
        }
    if hidden_low_value:
        return {
            **response,
            "items": out_items,
            "total": len(out_items),
            "policy_display_translation": {
                "translated": 0,
                "from_cache": 0,
                "limit": POLICY_DISPLAY_TRANSLATION_LIMIT,
                "hidden_low_value": hidden_low_value,
            },
        }
    return {**response, "items": out_items, "total": len(out_items)}


async def _apply_policy_display_translations(response: dict) -> dict:
    """只在返回给前端前翻译英文政策条目，不改抓取结果和数据库原文。"""
    return await run_in_threadpool(_apply_policy_display_translations_sync, response)


async def _finalize_logistics_response(response: dict, remember: bool = False) -> dict:
    response = await _apply_policy_display_translations(response)
    if remember:
        return _remember_logistics_response(response)
    return response


def _is_useful_feed_item(title: str, url: str) -> bool:
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    path = parsed.path or "/"
    blocked_url_patterns = [
        r"wl123\.com/(?:company|sites?|tools?|jobs?)/",
        r"cifnews\.com/(?:product|service|course|ask)/",
        r"ustr\.gov/trade-agreements/(?:agreements-reciprocal-trade|free-trade-agreements|trade-investment-framework-agreements|bilateral-investment-treaties)",
        r"ship\.sh/(?:about|contact)",
        r"/(?:login|register|logout)(?:/|$)",
    ]
    if any(re.search(pattern, url, re.I) for pattern in blocked_url_patterns):
        return False
    if path in {"", "/"} and re.search(r"首页|导航|平台$", title):
        return False
    blocked_title_patterns = [
        r"^[\w.+-]+@[\w.-]+$",
        r"开店\s+.*站点$",
        r"汽车后市场|汽车流通消费|造船龙头|全球开店季",
        r"^(?:Free Trade Agreements|Trade & Investment Framework Agreements|Bilateral Investment Treaties|Agreements on Reciprocal Trade)$",
        r"找服务|访问官网|扫码咨询",
    ]
    return not any(re.search(pattern, title, re.I) for pattern in blocked_title_patterns)


def _logistics_data_dirs() -> list[Path]:
    candidates = [ROOT / "data", get_data_dir()]
    paths: list[Path] = []
    seen = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(path)
    return paths


def _logistics_cache_paths() -> list[Path]:
    return [data_dir / "logistics_cache.json" for data_dir in _logistics_data_dirs()]


def _normalize_logistics_payload(
    payload: dict,
    default_task: str = "",
    default_label: str = "",
) -> dict:
    """统一实时抓取和 cron 缓存的字段，前端只读这一种结构。"""
    items = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        task = (
            raw.get("task")
            or raw.get("_task")
            or raw.get("source_module")
            or raw.get("task_type")
            or default_task
        )
        label = (
            raw.get("label")
            or raw.get("_label")
            or raw.get("_task_label")
            or default_label
            or TASK_LABELS.get(task)
        )
        raw_body_text = raw.get("body_text") or raw.get("content_snippet") or ""
        summary = raw.get("summary") or raw.get("ai_summary") or raw_body_text or raw.get("ai_analysis") or ""
        score = raw.get("score", raw.get("ai_score", 0))
        tags = raw.get("tags", raw.get("ai_tags", [])) or []
        source_url = raw.get("source_url") or raw.get("url") or "#"
        title = _clean_feed_title(raw.get("title", ""))
        if not title or source_url == "#" or not _is_useful_feed_item(title, source_url):
            continue
        summary = _clean_common_summary(summary, title)
        if len(summary) < 30 and raw_body_text:
            summary = _clean_common_summary(raw_body_text, title)
        # 过滤无实质内容的条目（导航页、空内容等）
        if not summary or len(summary) < 30:
            continue

        # 清理摘要中的网页噪音
        _noise_phrases = [
            "用小程序打开更快", "打开APP", "下载APP", "立即下载",
            "点击查看", "阅读全文", "展开全文", "收起",
            "登录后查看", "注册免费", "免费注册", "立即注册",
            "扫码关注", "关注我们", "微信扫码", "复制链接",
            "分享到", "转发到", "版权声明", "免责声明",
        ]
        for noise in _noise_phrases:
            summary = summary.replace(noise, "")
        summary = re.sub(r"\s*[-–—|｜]\s*\S{2,8}(电商|物流|网|平台|资讯)\s*$", "", summary)
        summary = _clean_common_summary(summary, title)
        if task == "by56-wiki":
            summary = _clean_by56_summary(summary, title)
        body_text = raw_body_text
        analysis = ""
        if task == "by56-wiki":
            analysis = _build_by56_analysis(title, summary, body_text)
        if _is_recruitment_item(title, summary, source_url):
            continue

        # 过滤"摘要≈标题"：如果去掉标点后摘要和标题高度相似，说明没有真正内容
        _title_clean = re.sub(r"[^\w]", "", title)
        _summary_clean = re.sub(r"[^\w]", "", summary)
        if _title_clean and _summary_clean:
            overlap = len(set(_title_clean) & set(_summary_clean)) / max(len(set(_title_clean)), 1)
            if overlap > 0.85 and len(_summary_clean) < len(_title_clean) * 1.5:
                continue

        # 清理后再判断摘要长度
        summary = summary.strip()
        if len(summary) < 30:
            continue

        # 标题精简：超过 60 字截断，多余部分前置到摘要
        if len(title) > 60:
            cut = title[:60].rfind("，")
            if cut < 20:
                cut = title[:60].rfind("、")
            if cut < 20:
                cut = 58
            overflow = title[cut + 1:].strip()
            title = title[: cut + 1].rstrip("，、") + "…"
            if overflow and overflow not in summary:
                summary = overflow + " " + summary

        # 摘要精简：去掉重复短行、清洗噪音、限制 200 字
        summary = " ".join(
            line.strip()
            for line in summary.split("\n")
            if line.strip() and len(line.strip()) > 12
        )
        summary = re.sub(r"\s{2,}", " ", summary).strip()
        if len(summary) > 500:
            cut_pos = summary[:500].rfind("。")
            if cut_pos < 80:
                cut_pos = summary[:200].rfind("，")
            if cut_pos < 80:
                cut_pos = 198
            summary = summary[: cut_pos + 1] + "…"

        item = {
            **raw,
            "title": title,
            "source_name": raw.get("source_name") or raw.get("source_id") or "?",
            "source_url": source_url,
            "summary": summary,
            "ai_summary": raw.get("ai_summary") or summary,
            "analysis": analysis,
            "ai_analysis": analysis,
            "score": score,
            "ai_score": raw.get("ai_score", score),
            "tags": tags,
            "ai_tags": raw.get("ai_tags", tags),
            "task": task,
            "label": label,
            "_task_label": raw.get("_task_label") or label,
            "image": raw.get("image") or raw.get("thumbnail") or raw.get("og_image") or "",
        }
        items.append(item)

    counts_by_task: dict[str, int] = {}
    for item in items:
        task_key = item.get("task") or ""
        if task_key:
            counts_by_task[task_key] = counts_by_task.get(task_key, 0) + 1

    sources = []
    for source in payload.get("sources") or []:
        if not isinstance(source, dict):
            continue
        error = source.get("error")
        task_type = source.get("task_type", "")
        count = counts_by_task.get(task_type, 0)
        sources.append({
            **source,
            "label": source.get("label") or TASK_LABELS.get(task_type, task_type),
            "count": count,
            "ok": count > 0,
        })

    return {
        **payload,
        "updated_at": payload.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        "total": len(items),
        "sources": sources,
        "items": items,
    }


def _payload_date(payload: dict) -> str:
    for key in ("data_date", "date"):
        value = payload.get(key)
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
    updated_at = str(payload.get("updated_at") or "")
    if re.match(r"\d{4}-\d{2}-\d{2}", updated_at):
        return updated_at[:10]
    return ""


def _load_logistics_cache() -> dict | None:
    for path in _logistics_cache_paths():
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            normalized = _normalize_logistics_payload(payload)
            if normalized.get("items"):
                normalized.setdefault("data_date", _payload_date(normalized))
                return normalized
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _save_logistics_cache(payload: dict) -> None:
    normalized = _normalize_logistics_payload(payload)
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "logistics_cache.json"
    tmp = target.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    tmp.replace(target)
    # 同时保存按日期命名的快照
    today_str = today_local()
    daily_target = data_dir / f"logistics_{today_str}.json"
    try:
        with open(daily_target, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    # 清理超期旧快照
    _cleanup_old_cache_files()


def _load_logistics_by_date(date_str: str) -> dict | None:
    """加载指定日期的物流快照"""
    for data_dir in _logistics_data_dirs():
        path = data_dir / f"logistics_{date_str}.json"
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            normalized = _normalize_logistics_payload(payload)
            if normalized.get("items"):
                normalized["date"] = date_str
                normalized["data_date"] = date_str
                return normalized
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _list_available_dates() -> list[str]:
    """列出已有日报快照的日期（降序）"""
    dates = set()
    for data_dir in _logistics_data_dirs():
        if not data_dir.exists():
            continue
        for f in data_dir.glob("logistics_*.json"):
            name = f.stem
            if name == "logistics_cache":
                continue
            date_part = name.replace("logistics_", "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part):
                dates.add(date_part)
    return sorted(dates, reverse=True)


def _cleanup_old_cache_files() -> int:
    """自动删除超过 _CACHE_FILE_KEEP_DAYS 天的旧日期快照，返回清理数量"""
    data_dir = get_data_dir()
    if not data_dir.exists():
        return 0
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_CACHE_FILE_KEEP_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for f in data_dir.glob("logistics_*.json"):
        name = f.stem
        if name == "logistics_cache":
            continue
        date_part = name.replace("logistics_", "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part) and date_part < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


async def _fetch_og_image(url: str, client: httpx.AsyncClient) -> str:
    """从原文页面提取 og:image 封面图"""
    if not url or not url.startswith("http"):
        return ""
    try:
        resp = await client.get(url, timeout=8, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text[:20000]
        # 快速正则提取，避免 import BeautifulSoup
        for pattern in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        ]:
            m = re.search(pattern, html, re.I)
            if m and m.group(1).startswith("http"):
                return m.group(1)
    except Exception:
        pass
    return ""


async def _enrich_ai_items_with_images(items: list[dict]) -> list[dict]:
    """为 AI HOT 条目并发抓取 og:image 封面图（限制并发避免过载）"""
    if not items:
        return items

    sem = asyncio.Semaphore(6)

    async def fetch_one(item, client):
        async with sem:
            url = item.get("url") or item.get("sourceUrl") or ""
            img = await _fetch_og_image(url, client)
            if img:
                item["image"] = img
            return item

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    ) as client:
        results = await asyncio.gather(*[fetch_one(i, client) for i in items])
    return list(results)


async def _fetch_from_aihot() -> dict:
    """从 AI HOT 官方 REST API 拉取精选条目（无需 token，轻量调用）"""
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=_AIHOT_HEADERS) as client:
        endpoints = [
            ("/api/public/items", {"mode": "selected", "limit": 50}),
            ("/api/public/daily", None),
        ]
        for path, params in endpoints:
            try:
                resp = await client.get(f"{API_BASE}{path}", params=params)
                resp.raise_for_status()
                data = _normalize_aihot_payload(resp.json())
                if data.get("items"):
                    _save_ai_cache(data)
                    break
                last_error = ValueError(f"AI HOT {path} returned empty items")
            except Exception as exc:
                last_error = exc
        else:
            raise last_error or RuntimeError("AI HOT returned no usable data")

    # 先返回数据（不等图片），后台异步补充封面图
    items = data.get("items") or []
    if items:
        asyncio.ensure_future(_bg_enrich_ai_images(items))
    return data


async def _bg_enrich_ai_images(items: list[dict]):
    """后台任务：异步补充 AI 条目封面图，完成后更新内存缓存"""
    global _cache_ai, _cache_time_ai
    try:
        enriched = await _enrich_ai_items_with_images(items)
        if _cache_ai and _cache_ai.get("items"):
            _cache_ai["items"] = enriched
    except Exception:
        pass


async def _scrape_logistics() -> dict:
    """直接调用抓取管道（不经 HTTP 自调用），聚合所有物流任务类型。
    每个源独立超时 + 重试，单个源失败不影响其他源。
    """
    from routes.scrape import FetchRequest, fetch_pipeline  # noqa: E402

    _TIMEOUT_PER_SOURCE = 45  # 单个源最多等待 45 秒
    _MAX_RETRIES = 2  # 失败最多重试 1 次（共 2 次尝试）

    all_items = []
    all_errors = []
    sources_report = []

    async def fetch_one(task_type, label):
        last_error = ""
        for attempt in range(_MAX_RETRIES):
            try:
                req = FetchRequest(
                    task_type=task_type,
                    limit=12,
                    force_refresh=True,
                    recency_days=5,
                    analyze_with_llm=False,
                    screen_with_llm=False,
                )
                result = await asyncio.wait_for(
                    fetch_pipeline(req), timeout=_TIMEOUT_PER_SOURCE
                )
                candidates = [c.model_dump(mode="json") if hasattr(c, "model_dump") else c
                              for c in (result.candidates if hasattr(result, "candidates") else result.get("candidates", []))]
                for c in candidates:
                    c["task"] = task_type
                    c["label"] = label
                    c["_task_label"] = label
                errors = result.errors if hasattr(result, "errors") else result.get("errors", [])
                return {
                    "label": label, "task_type": task_type,
                    "count": len(candidates), "items": candidates,
                    "errors": errors,
                }
            except asyncio.TimeoutError:
                last_error = f"{task_type} 超时({_TIMEOUT_PER_SOURCE}s)"
            except Exception as e:
                last_error = f"{task_type}: {e}"
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(3)  # 重试前短暂等待
        return {"label": label, "task_type": task_type, "count": 0, "items": [], "errors": [last_error]}

    results = await asyncio.gather(*[fetch_one(t, l) for t, l in LOGISTICS_TASKS])
    for r in results:
        all_items.extend(r.get("items", []))
        all_errors.extend(r.get("errors", []))
        sources_report.append({
            "label": r["label"], "task_type": r["task_type"],
            "count": r["count"],
            "error": r["errors"][0] if r["errors"] and not r["items"] else None,
        })

    return _normalize_logistics_payload({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(all_items),
        "sources": sources_report,
        "items": all_items,
        "errors": all_errors[-10:] if all_errors else [],
    })


def _logistics_items_for_db(raw_items: list[dict]) -> list[dict]:
    db_ready = []
    for item in raw_items:
        db_ready.append({
            "title": item.get("title", ""),
            "source_name": item.get("source_name") or item.get("source_id", ""),
            "source_url": item.get("source_url") or item.get("url", ""),
            "summary": item.get("summary") or item.get("ai_summary", ""),
            "body_text": item.get("body_text") or item.get("content_snippet", ""),
            "image": item.get("image", ""),
            "ai_score": item.get("ai_score") or item.get("score", 5.0),
            "ai_tags": item.get("ai_tags") or item.get("tags", []),
            "task_type": item.get("task") or item.get("task_type", ""),
        })
    return db_ready


async def _persist_logistics_payload(payload: dict) -> str:
    """把抓取结果同时写入文件快照和 SQLite。返回非致命写入错误。"""
    errors = []
    raw_items = payload.get("items") or []
    if not raw_items:
        return ""

    try:
        await run_in_threadpool(_save_logistics_cache, payload)
    except Exception as exc:
        errors.append(f"文件缓存写入失败：{exc}")

    db_ready = _logistics_items_for_db(raw_items)
    if db_ready:
        try:
            from db import insert_items
            await run_in_threadpool(insert_items, db_ready, True)
        except Exception as exc:
            errors.append(f"数据库写入失败：{exc}")
    return "；".join(errors)


def _remember_logistics_response(response: dict) -> dict:
    global _cache_logistics, _cache_time_logistics
    _cache_logistics = response
    _cache_time_logistics = time.time()
    return response


def _format_logistics_cache_response(
    payload: dict,
    query_date: str,
    source: str,
    error: str = "",
) -> dict:
    data_date = _payload_date(payload)
    resp = _normalize_logistics_payload(payload)
    resp.update({
        "date": query_date,
        "from_db": False,
        "from_cache": True,
        "cache_source": source,
        "cached": True,
        "updated_at": resp.get("updated_at") or datetime.now(timezone.utc).isoformat(),
    })
    if data_date:
        resp["data_date"] = data_date
        if data_date != query_date:
            resp["stale"] = True
    if error:
        resp["error"] = error
    return resp


def _load_logistics_fallback(query_date: str) -> tuple[dict | None, str]:
    dated = _load_logistics_by_date(query_date)
    if dated:
        return dated, "date_snapshot"
    latest = _load_logistics_cache()
    if latest:
        return latest, "latest_snapshot"
    return None, ""


def _response_data_age_seconds(response: dict) -> float | None:
    updated_at = str(response.get("updated_at") or "")
    if not updated_at:
        return None
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - dt.timestamp())
    except (ValueError, TypeError):
        return None


def _maybe_schedule_stale_refresh(response: dict, query_date: str, today: str, refresh: bool) -> None:
    """页面访问时若今日数据超过阈值未更新，后台静默触发抓取。
    仅北京时间首档～末档后 1 小时内允许，晚上不补抓。
    """
    if refresh or query_date != today:
        return
    if not allow_page_stale_refresh():
        return
    if response.get("stale"):
        _schedule_logistics_refresh("stale_page_load")
        return
    age = _response_data_age_seconds(response)
    if age is None or age >= _LOGISTICS_STALE_TRIGGER_SECONDS:
        _schedule_logistics_refresh("stale_page_load")


async def _finalize_and_return_logistics(
    response: dict,
    *,
    remember: bool = False,
    query_date: str = "",
    today: str = "",
    refresh: bool = False,
) -> dict:
    finalized = await _finalize_logistics_response(response, remember=remember)
    _maybe_schedule_stale_refresh(finalized, query_date, today, refresh)
    # 今日有数据时后台补排版（已排版的跳过，不重复烧 token）
    if query_date and today and query_date == today and (finalized.get("total") or 0) > 0:
        _schedule_featured_auto_format(query_date, "logistics_page_load")
    return finalized


def _schedule_logistics_refresh(reason: str = "") -> None:
    """用户请求只负责秒回；刷新和入库交给后台单任务。"""
    global _logistics_refresh_task, _logistics_refresh_started_at
    if _logistics_refresh_task and not _logistics_refresh_task.done():
        age = time.time() - _logistics_refresh_started_at if _logistics_refresh_started_at else 0
        if age < _LOGISTICS_REFRESH_STALE_SECONDS:
            return
        _logistics_refresh_task.cancel()
    _logistics_refresh_started_at = time.time()
    _logistics_refresh_task = asyncio.create_task(_refresh_logistics_background(reason))


def _invalidate_logistics_cache() -> None:
    global _cache_logistics, _cache_time_logistics
    _cache_logistics = None
    _cache_time_logistics = 0


def _schedule_featured_auto_format(date: str, reason: str = "") -> None:
    """抓取入库后：精选 Top5 + 行业/政策逐条排版（每条至多一次）；不阻塞用户请求。"""
    global _featured_auto_task, _featured_auto_started_at
    if not date:
        return
    if _featured_auto_task and not _featured_auto_task.done():
        age = time.time() - _featured_auto_started_at if _featured_auto_started_at else 0
        if age < _FEATURED_AUTO_COOLDOWN_SECONDS:
            return
    _featured_auto_started_at = time.time()
    _featured_auto_task = asyncio.create_task(_post_refresh_llm_background(date, reason))


async def _post_refresh_llm_background(date: str, reason: str = "") -> None:
    import logging

    featured_log = logging.getLogger("featured.auto")
    ops_log = logging.getLogger("ops_format.auto")
    try:
        if not is_manual_featured(date):
            result = await run_in_threadpool(auto_format_featured, date)
            if result.get("skipped"):
                featured_log.info(
                    "自动精选跳过 date=%s reason=%s skip=%s",
                    date,
                    reason,
                    result.get("skip_reason"),
                )
            else:
                featured_log.info(
                    "自动精选完成 date=%s reason=%s items=%d source=%s",
                    date,
                    reason,
                    len(result.get("items") or []),
                    result.get("source"),
                )
        else:
            featured_log.info("今日精选已人工定稿，跳过自动排版 date=%s", date)
    except Exception as exc:
        featured_log.warning("自动精选失败 date=%s reason=%s error=%s", date, reason, exc, exc_info=True)

    try:
        ops = await run_in_threadpool(auto_format_ops_items, date)
        if ops.get("skipped"):
            ops_log.info(
                "行业/政策排版跳过 date=%s reason=%s skip=%s",
                date,
                reason,
                ops.get("skip_reason"),
            )
        else:
            ops_log.info(
                "行业/政策排版完成 date=%s reason=%s processed=%d ok=%d failed=%d",
                date,
                reason,
                ops.get("processed", 0),
                ops.get("ok", 0),
                ops.get("failed", 0),
            )
            if ops.get("ok", 0) > 0:
                _invalidate_logistics_cache()
    except Exception as exc:
        ops_log.warning("行业/政策排版失败 date=%s reason=%s error=%s", date, reason, exc, exc_info=True)


async def _featured_auto_format_background(date: str, reason: str = "") -> None:
    """兼容旧调用名。"""
    await _post_refresh_llm_background(date, reason)


async def _refresh_logistics_background(reason: str = "") -> None:
    global _logistics_refresh_started_at, _last_logistics_refresh
    import logging
    log = logging.getLogger("logistics.refresh")
    started = time.time()
    featured_day = ""
    _last_logistics_refresh = {
        **_last_logistics_refresh,
        "ok": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": "",
        "duration_sec": None,
        "reason": reason,
        "error": "",
    }
    try:
        async with _lock_logistics:
            try:
                scraped = await asyncio.wait_for(
                    _scrape_logistics(),
                    timeout=_LOGISTICS_REFRESH_TOTAL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "后台刷新总超时 reason=%s timeout=%ss",
                    reason,
                    _LOGISTICS_REFRESH_TOTAL_TIMEOUT,
                )
                _last_logistics_refresh.update({
                    "ok": False,
                    "error": f"刷新总超时 {_LOGISTICS_REFRESH_TOTAL_TIMEOUT}s",
                })
                return
            if not scraped.get("items"):
                log.warning("后台刷新完成但无有效条目 reason=%s errors=%s", reason, scraped.get("errors", []))
                _last_logistics_refresh.update({
                    "ok": False,
                    "error": "无有效条目；" + "；".join(scraped.get("errors", [])[:3]),
                    "items": 0,
                })
                return
            write_error = await _persist_logistics_payload(scraped)
            if write_error:
                log.warning("数据持久化部分失败: %s", write_error)
            today = today_local()
            try:
                from db import get_published
                db_items = await run_in_threadpool(get_published, date=today, limit=200)
            except Exception:
                db_items = []
            db_items, fill_meta = await _supplement_db_items_by_task(db_items, today)
            if db_items:
                response = _format_logistics_response(db_items, today, from_db=True, error=write_error)
                if fill_meta.get("added"):
                    response["fallback_fill"] = fill_meta
                    response["stale_mixed"] = True
            else:
                response = _format_logistics_cache_response(
                    scraped,
                    today,
                    source="background_refresh",
                    error=write_error,
                )
            _remember_logistics_response(response)
            counts_by_task: dict[str, int] = {}
            for item in response.get("items", []):
                task = item.get("task") or item.get("task_type") or "other"
                counts_by_task[task] = counts_by_task.get(task, 0) + 1
            _last_logistics_refresh.update({
                "ok": True,
                "error": write_error or "",
                "items": len(response.get("items", [])),
                "counts_by_task": counts_by_task,
                "fallback_added": (response.get("fallback_fill") or {}).get("added", 0),
            })
            featured_day = today
            log.info("后台刷新成功 reason=%s items=%d", reason, len(scraped.get("items", [])))
    except asyncio.CancelledError:
        log.warning("后台刷新任务被取消 reason=%s", reason)
        _last_logistics_refresh.update({"ok": False, "error": "刷新任务被取消"})
        raise
    except Exception as exc:
        log.error("后台刷新异常 reason=%s error=%s", reason, exc, exc_info=True)
        _last_logistics_refresh.update({"ok": False, "error": str(exc)})
        # 后台刷新失败不影响用户；定时任务或下次请求会重试
    finally:
        _last_logistics_refresh.update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_sec": round(time.time() - started, 2),
        })
        _logistics_refresh_started_at = 0
    if featured_day:
        _schedule_featured_auto_format(featured_day, reason or "logistics_refresh")


async def _supplement_db_items_by_task(
    db_items: list[dict],
    query_date: str,
    min_per_task: int = MIN_LOGISTICS_ITEMS_PER_TASK,
    max_fallback_per_task: int = MAX_LOGISTICS_FALLBACK_PER_TASK,
    max_fallback_total: int = MAX_LOGISTICS_FALLBACK_TOTAL,
) -> tuple[list[dict], dict]:
    """按任务模块从历史已发布库补足展示量。

    当当天某个模块抓不到或不足时，前台仍优先展示当天数据，再用历史 published
    兜底补齐，避免用户打开晨间星闻时某个板块直接空掉。
    """
    if min_per_task <= 0:
        return db_items, {"enabled": False, "added": 0, "min_per_task": min_per_task}

    from db import get_published

    supplemented = list(db_items or [])
    seen_urls = {
        str(item.get("source_url") or item.get("url") or "")
        for item in supplemented
        if item.get("source_url") or item.get("url")
    }
    added_by_task: dict[str, int] = {}
    total_fallback_added = 0

    def valid_for_task(item: dict, task_type: str) -> bool:
        if (item.get("task_type") or item.get("task")) != task_type:
            return False
        if task_type in POLICY_DISPLAY_TASKS and _is_low_value_policy_item(item):
            return False
        return True

    for task_type, _label in LOGISTICS_TASKS:
        if max_fallback_total > 0 and total_fallback_added >= max_fallback_total:
            break

        current_count = sum(1 for item in supplemented if valid_for_task(item, task_type))
        if current_count >= min_per_task:
            continue

        need = min_per_task - current_count
        if max_fallback_per_task > 0:
            need = min(need, max_fallback_per_task)
        if max_fallback_total > 0:
            need = min(need, max_fallback_total - total_fallback_added)
        if need <= 0:
            continue

        try:
            historical = await run_in_threadpool(get_published, task_type=task_type, limit=max(100, min_per_task * 5))
        except Exception:
            historical = []

        for item in historical:
            url = str(item.get("source_url") or item.get("url") or "")
            if not url or url in seen_urls:
                continue
            item = dict(item)
            if not valid_for_task(item, task_type):
                continue
            item["_fallback_from_history"] = True
            item["_fallback_for_date"] = query_date
            supplemented.append(item)
            seen_urls.add(url)
            added_by_task[task_type] = added_by_task.get(task_type, 0) + 1
            total_fallback_added += 1
            need -= 1
            if need <= 0:
                break
            if max_fallback_total > 0 and total_fallback_added >= max_fallback_total:
                break

    return supplemented, {
        "enabled": True,
        "min_per_task": min_per_task,
        "max_fallback_per_task": max_fallback_per_task,
        "max_fallback_total": max_fallback_total,
        "added": sum(added_by_task.values()),
        "added_by_task": added_by_task,
    }


@router.get("")
async def get_briefing(refresh: bool = False):
    """获取科技动态最新素材"""
    global _cache_ai, _cache_time_ai
    now = time.time()
    # 缓存有效期内直接返回（无锁，高并发安全）
    if not refresh and _cache_ai and (now - _cache_time_ai) < _CACHE_TTL:
        return {**_cache_ai, "cached": True, "cache_age_sec": int(now - _cache_time_ai)}
    # 加锁：同一时刻只有一个协程执行抓取，其余等待后读缓存
    async with _lock_ai:
        now = time.time()
        if not refresh and _cache_ai and (now - _cache_time_ai) < _CACHE_TTL:
            return {**_cache_ai, "cached": True, "cache_age_sec": int(now - _cache_time_ai)}
        try:
            data = await _fetch_from_aihot()
            data = _normalize_aihot_payload(data)
            _cache_ai = data
            _cache_time_ai = time.time()
            return {**data, "cached": False}
        except Exception as e:
            error_text = str(e) or repr(e)
            if _cache_ai:
                return {**_cache_ai, "cached": True, "stale": True, "error": error_text}
            cached = _load_ai_cache()
            if cached:
                _cache_ai = cached
                _cache_time_ai = time.time()
                return {**cached, "cached": True, "stale": True, "error": error_text}
            return {"error": error_text, "items": []}


@router.get("/logistics/dates")
async def get_logistics_dates():
    """列出可用的日报日期（SQLite + 文件快照）"""
    from db import get_dates_with_data
    dates = set(_list_available_dates())
    try:
        dates.update(await run_in_threadpool(get_dates_with_data, 30))
    except Exception:
        pass
    today = today_local()
    dates.add(today)
    return {"dates": sorted(dates, reverse=True)[:30]}


@router.get("/logistics")
async def get_logistics(date: str = "", refresh: bool = False):
    """获取跨境行业与政策动态。

    普通访问只读已有数据：内存 → SQLite → 文件快照；抓取和入库放后台，避免用户打开页面时等待。
    refresh=True 才同步刷新。
    """
    from db import get_published

    today = today_local()
    query_date = resolve_query_date(date, default=today)
    now = time.time()

    if (
        not refresh
        and _cache_logistics
        and _cache_logistics.get("date") == query_date
        and (now - _cache_time_logistics) < _CACHE_TTL
    ):
        return await _finalize_and_return_logistics(
            {**_cache_logistics, "cached": True, "cache_age_sec": int(now - _cache_time_logistics)},
            query_date=query_date,
            today=today,
            refresh=refresh,
        )

    db_error = ""
    try:
        db_items = await run_in_threadpool(get_published, date=query_date, limit=200)
    except Exception as exc:
        db_items = []
        db_error = str(exc)

    if not refresh:
        db_items, fill_meta = await _supplement_db_items_by_task(db_items, query_date)
        if db_items:
            response = _format_logistics_response(db_items, query_date, from_db=True, error=db_error)
            if fill_meta.get("added"):
                response["fallback_fill"] = fill_meta
                response["stale_mixed"] = True
            return await _finalize_and_return_logistics(
                response,
                remember=True,
                query_date=query_date,
                today=today,
                refresh=refresh,
            )

    if not refresh:
        fallback, source = _load_logistics_fallback(query_date)
        if fallback:
            if query_date == today and allow_page_stale_refresh():
                _schedule_logistics_refresh("fallback-returned")
            return await _finalize_and_return_logistics(
                _format_logistics_cache_response(fallback, query_date, source=source, error=db_error),
                remember=True,
                query_date=query_date,
                today=today,
                refresh=refresh,
            )
        if query_date == today and allow_page_stale_refresh():
            _schedule_logistics_refresh("no-cache")
        return {
            "items": [],
            "total": 0,
            "date": query_date,
            "refreshing": query_date == today and allow_page_stale_refresh(),
            "error": db_error or f"暂无 {query_date} 的缓存数据，后台正在刷新",
        }

    if query_date != today:
        fallback, source = _load_logistics_fallback(query_date)
        if fallback:
            return await _finalize_logistics_response(
                _format_logistics_cache_response(fallback, query_date, source=source, error=db_error)
            )
        return {"items": [], "total": 0, "date": query_date, "error": f"暂无 {query_date} 的数据"}

    async with _lock_logistics:
        try:
            scraped = await _scrape_logistics()
            write_error = await _persist_logistics_payload(scraped)
            db_items = await run_in_threadpool(get_published, date=query_date, limit=200)
            db_items, fill_meta = await _supplement_db_items_by_task(db_items, query_date)
            if db_items:
                response = _format_logistics_response(db_items, query_date, from_db=True, error=write_error)
                if fill_meta.get("added"):
                    response["fallback_fill"] = fill_meta
                    response["stale_mixed"] = True
                _schedule_featured_auto_format(query_date, "live_refresh")
                return await _finalize_logistics_response(response, remember=True)
            if scraped.get("items"):
                _schedule_featured_auto_format(query_date, "live_refresh")
                return await _finalize_logistics_response(
                    _format_logistics_cache_response(
                        scraped,
                        query_date,
                        source="live_refresh",
                        error=write_error,
                    ),
                    remember=True,
                )
        except Exception as exc:
            fallback, source = _load_logistics_fallback(query_date)
            if fallback:
                return await _finalize_logistics_response(
                    _format_logistics_cache_response(fallback, query_date, source=source, error=str(exc))
                )
            return {"items": [], "total": 0, "date": query_date, "error": str(exc)}

    fallback, source = _load_logistics_fallback(query_date)
    if fallback:
        return await _finalize_logistics_response(
            _format_logistics_cache_response(fallback, query_date, source=source, error="刷新未返回有效条目")
        )
    return {"items": [], "total": 0, "date": query_date, "error": "刷新未返回有效条目"}


def _format_logistics_response(db_items: list[dict], date: str, from_db: bool = False, error: str = "") -> dict:
    """将数据库行格式化为前端需要的结构"""
    raw_items = []
    for i in db_items:
        task = i.get("task_type", "other")
        raw_items.append({
            "title": i.get("title", ""),
            "source_name": i.get("source_name", ""),
            "source_url": i.get("source_url", ""),
            "summary": i.get("summary", ""),
            "body_text": i.get("body_text", ""),
            "analysis": i.get("analysis", ""),
            "ai_summary": i.get("summary", ""),
            "ai_analysis": i.get("analysis", ""),
            "image": i.get("image", ""),
            "score": i.get("ai_score", 0),
            "ai_score": i.get("ai_score", 0),
            "tags": i.get("ai_tags", []),
            "ai_tags": i.get("ai_tags", []),
            "task": task,
            "label": TASK_LABELS.get(task, task),
            "_task_label": TASK_LABELS.get(task, task),
        })

    normalized = _normalize_logistics_payload({
        "items": raw_items,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    resp = {
        **normalized,
        "date": date,
        "from_db": from_db,
    }
    if error:
        resp["error"] = error
    return resp


class FeaturedFinalizeRequest(BaseModel):
    source_urls: list[str] = Field(default_factory=list, description="定稿条目原文链接，最多 5 条")
    date: str = Field(default="", description="YYYY-MM-DD，默认今天")
    use_llm: bool = Field(default=True, description="定稿后是否统一过 LLM 生成发生了什么/影响")


@router.get("/featured")
async def get_featured(date: str = ""):
    """今日精选：已定稿/自动排版则返回成品；否则规则 Top5 占位并触发后台 DeepSeek 排版。"""
    query_date = resolve_query_date(date)
    payload = await run_in_threadpool(get_featured_response, query_date)
    if (
        query_date == today_local()
        and not payload.get("finalized")
        and payload.get("needs_auto_format")
        and payload.get("total", 0) > 0
    ):
        _schedule_featured_auto_format(query_date, "featured_page_load")
    return payload


@router.get("/featured/candidates")
async def get_featured_candidates(date: str = "", _user: str | None = Depends(require_auth)):
    """今日精选候选池（加权排序，供管理端勾选）。"""
    query_date = resolve_query_date(date)
    candidates = await run_in_threadpool(build_featured_candidates, query_date)
    store = await run_in_threadpool(load_featured_store, query_date)
    selected_urls = []
    if store and store.get("items"):
        selected_urls = [
            str(item.get("source_url") or "").strip()
            for item in store.get("items") or []
            if item.get("source_url")
        ]
    return {
        "date": query_date,
        "items": candidates,
        "total": len(candidates),
        "finalized": bool(store and store.get("finalized")),
        "selected_urls": selected_urls,
    }


@router.post("/featured")
async def post_featured(req: FeaturedFinalizeRequest, _user: str | None = Depends(require_auth)):
    """定稿今日精选（≤5 条），并统一过 LLM 生成发生了什么/影响。"""
    if not req.source_urls:
        return {"ok": False, "error": "请至少选择 1 条候选"}
    query_date = resolve_query_date(req.date)
    payload = await run_in_threadpool(
        finalize_featured,
        req.source_urls,
        query_date,
        req.use_llm,
    )
    return {
        "ok": True,
        "date": query_date,
        "total": len(payload.get("items") or []),
        "items": payload.get("items") or [],
        "missing_urls": payload.get("missing_urls") or [],
        "finalized": True,
    }


@router.post("/refresh")
async def refresh_briefing(_user: str | None = Depends(require_auth)):
    """强制刷新：重新抓取并写入数据库"""
    global _cache_ai, _cache_time_ai
    _cache_ai = None
    _cache_time_ai = 0
    data_ai = await get_briefing(refresh=True)
    data_log = await get_logistics(refresh=True)
    return {
        "ok": True,
        "ai_count": len(data_ai.get("items", [])),
        "logistics_count": data_log.get("total", 0),
        "fetched_at": datetime.now().isoformat(),
    }
