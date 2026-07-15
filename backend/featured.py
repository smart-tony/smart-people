"""
今日精选 — 规则初筛、定稿存储、LLM 统一格式化
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from db import get_today_items
from settings import get_data_dir, load_config, load_llm_config, today_local

_log = logging.getLogger("featured")
_featured_lock = threading.Lock()

ROOT = Path(__file__).resolve().parent.parent
FEATURED_TASKS = {
    "logistics-daily",
    "crossborder-platform",
    "shipping-port",
    "policy-official",
    "global-news",
}
POLICY_TASKS = {"policy-official", "global-news"}
INDUSTRY_TASKS = {"logistics-daily", "crossborder-platform", "shipping-port"}
EXCLUDED_TASKS = {"by56-wiki", "ai-weekly"}

_FRAGMENT_SUMMARY_RE = re.compile(r"^[a-eA-E][）)]")
_NOISE_OPENING_RE = re.compile(r"^(?:可以先不用改|分享至|当前位置|首页\s*>)")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_VALID_THEMES = {
    "清关合规",
    "海关政策",
    "关税贸易",
    "运价舱位",
    "港口突发",
    "平台物流",
    "出货风险",
}


def load_featured_weights() -> dict:
    return load_config("featured.weights.json", {})


def featured_path(date: str | None = None) -> Path:
    day = date or today_local()
    return get_data_dir() / f"featured_{day}.json"


def _item_text(item: dict) -> str:
    tags = item.get("ai_tags") or item.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = [tags]
    tag_text = " ".join(str(t) for t in tags)
    return " ".join(
        str(item.get(key) or "")
        for key in (
            "title",
            "summary",
            "body_text",
            "analysis",
            "source_name",
            "source_url",
        )
    ) + " " + tag_text


def _is_fragment_summary(text: str) -> bool:
    text = (text or "").strip()
    if len(text) < 18:
        return True
    if _FRAGMENT_SUMMARY_RE.search(text):
        return True
    if _NOISE_OPENING_RE.search(text):
        return True
    if text.startswith("）") or text.startswith(")"):
        return True
    return False


def classify_section(item: dict) -> str:
    task = item.get("task_type") or item.get("task") or ""
    if task in POLICY_TASKS:
        return "policy"
    return "industry"


def _detect_theme(text: str, weights: dict) -> tuple[str, float]:
    best_theme = ""
    best_score = 0.0
    for theme in weights.get("themes") or []:
        theme_id = str(theme.get("id") or "")
        score = float(theme.get("weight") or 0)
        hits = 0
        for keyword in theme.get("keywords") or []:
            if keyword and re.search(re.escape(keyword), text, re.I):
                hits += 1
        if hits:
            total = score + hits * 8
            if total > best_score:
                best_score = total
                best_theme = theme_id
    if not best_theme:
        return "出货风险", 0.0
    return best_theme, best_score


def _passes_hard_filters(item: dict, weights: dict | None = None) -> bool:
    task = item.get("task_type") or item.get("task") or ""
    if task not in FEATURED_TASKS:
        return False

    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or item.get("ai_summary") or "").strip()
    if not title:
        return False
    if _is_fragment_summary(summary):
        return False

    text = _item_text(item)
    weights = weights or load_featured_weights()
    for keyword in weights.get("exclude_keywords") or []:
        if keyword and re.search(re.escape(keyword), text, re.I):
            return False

    try:
        from routes.briefing import _is_low_value_policy_item

        if _is_low_value_policy_item(item):
            return False
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "featured: 无法加载低价值政策过滤，跳过该过滤: %s", exc
        )

    if _is_fragment_summary(title):
        return False
    return True


def score_candidate(item: dict, weights: dict | None = None) -> dict:
    weights = weights or load_featured_weights()
    text = _item_text(item)
    theme, theme_score = _detect_theme(text, weights)
    score = theme_score

    task = item.get("task_type") or item.get("task") or ""
    score += float((weights.get("task_boost") or {}).get(task, 0))

    source_url = str(item.get("source_url") or item.get("url") or "").lower()
    host = ""
    try:
        host = urlparse(source_url).netloc.replace("www.", "")
    except Exception:
        host = ""
    for pattern, boost in (weights.get("source_boost") or {}).items():
        if pattern in source_url or pattern in host:
            score += float(boost)

    ai_score = float(item.get("ai_score") or 0)
    if ai_score > 0:
        score += min(ai_score, 10) * 0.5

    priority = 9
    for theme_cfg in weights.get("themes") or []:
        if theme_cfg.get("id") == theme:
            priority = int(theme_cfg.get("priority", 9))
            break

    return {
        **item,
        "theme": theme,
        "featured_score": round(score, 2),
        "featured_priority": priority,
        "theme_score": theme_score,
        "section": classify_section(item),
    }


def build_featured_candidates(date: str | None = None, limit: int | None = None) -> list[dict]:
    weights = load_featured_weights()
    max_candidates = limit or int(weights.get("max_candidates") or 15)
    min_theme_score = float(weights.get("min_theme_score") or 0)
    day = date or today_local()
    raw_items = get_today_items(task_types=sorted(FEATURED_TASKS), limit=300, date=day)
    scored: list[dict] = []
    for item in raw_items:
        if not _passes_hard_filters(item, weights):
            continue
        row = score_candidate(item, weights)
        # 主题命中分过低 = 没有销售向信号，不进精选
        if float(row.get("theme_score") or 0) < min_theme_score:
            continue
        scored.append(row)

    scored.sort(
        key=lambda row: (
            row.get("featured_priority", 9),
            -float(row.get("featured_score") or 0),
            -float(row.get("ai_score") or 0),
        )
    )

    platform_max = int(weights.get("platform_logistics_max") or 1)
    picked: list[dict] = []
    platform_count = 0
    for row in scored:
        if row.get("theme") == "平台物流":
            if platform_count >= platform_max:
                continue
            platform_count += 1
        picked.append(_candidate_payload(row))
        if len(picked) >= max_candidates:
            break
    return picked


def _candidate_payload(item: dict) -> dict:
    task = item.get("task_type") or item.get("task") or ""
    return {
        "source_url": item.get("source_url") or item.get("url") or "",
        "title": item.get("title") or "",
        "summary": item.get("summary") or item.get("ai_summary") or "",
        "source_name": item.get("source_name") or "",
        "task_type": task,
        "task": task,
        "section": item.get("section") or classify_section(item),
        "theme": item.get("theme") or "",
        "featured_score": item.get("featured_score") or 0,
        "featured_priority": item.get("featured_priority") or 9,
        "ai_score": item.get("ai_score") or 0,
    }


def load_featured_store(date: str | None = None) -> dict | None:
    path = featured_path(date)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_featured_store(payload: dict, date: str | None = None) -> dict:
    import os
    import tempfile

    day = date or today_local()
    path = featured_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **payload,
        "date": day,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    fd, tmp_name = tempfile.mkstemp(prefix="featured_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return payload


def _rule_based_featured(date: str | None = None) -> list[dict]:
    max_items = int(load_featured_weights().get("max_items") or 5)
    candidates = build_featured_candidates(date=date, limit=max_items)
    items = []
    for row in candidates:
        items.append({
            **_candidate_payload(row),
            "what_happened": "",
            "impact": "",
            "finalized": False,
            "llm_ok": False,
        })
    return items


def _lookup_today_items_by_urls(urls: list[str], date: str | None = None) -> dict[str, dict]:
    wanted = {u.strip() for u in urls if u and u.strip()}
    if not wanted:
        return {}
    day = date or today_local()
    raw_items = get_today_items(task_types=sorted(FEATURED_TASKS), limit=300, date=day)
    found: dict[str, dict] = {}
    for item in raw_items:
        url = (item.get("source_url") or item.get("url") or "").strip()
        if url in wanted and url not in found:
            found[url] = item
    return found


def _strip_json_fence(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _body_excerpt(item: dict, max_chars: int = 1800) -> str:
    body = str(item.get("body_text") or "").strip()
    summary = str(item.get("summary") or item.get("ai_summary") or "").strip()
    text = body or summary
    try:
        from routes.scrape import strip_reading_meta

        text = strip_reading_meta(text)
    except Exception:
        pass
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for sep in ("。", "！", "？", ".", " "):
        pos = cut.rfind(sep)
        if pos > max_chars * 0.55:
            return cut[: pos + 1]
    return cut + "…"


def format_featured_item_with_llm(item: dict) -> dict:
    llm_config = load_llm_config()
    api_key = str(llm_config.get("api_key") or "")
    if not api_key or api_key == "***":
        return _fallback_featured_item(item, llm_error="LLM 未配置")

    try:
        from routes.llm import get_llm_client
    except Exception as exc:
        return _fallback_featured_item(item, llm_error=str(exc))

    client = get_llm_client()
    if client is None:
        return _fallback_featured_item(item, llm_error="LLM 客户端不可用")

    title = str(item.get("title") or "").strip()
    source_name = str(item.get("source_name") or "").strip()
    source_url = str(item.get("source_url") or item.get("url") or "").strip()
    body_excerpt = _body_excerpt(item)

    system_prompt = (
        "你是百运网晨间精选编辑，面向跨境物流销售与客服。"
        "只基于原文提炼核心事实，不编造、不营销话术。"
        "文案要整洁、短句、可扫读。输出严格 JSON，不要 markdown。"
    )
    user_prompt = (
        "请把下面情报整理成晨间精选卡片，输出 JSON：\n"
        "{\n"
        '  "title": "18-32字，有主语，去掉感叹号堆砌与「注意/分享至」等噪音",\n'
        '  "what_happened": "1-2句核心事实：谁/哪条航线或政策/发生了什么变化",\n'
        '  "impact": "1-2句业务影响：出货、清关、运价、舱位、时效或客户沟通；不确定写「需结合原文人工确认」",\n'
        '  "theme": "清关合规|海关政策|关税贸易|运价舱位|港口突发|平台物流|出货风险"\n'
        "}\n\n"
        f"来源：{source_name}\n"
        f"链接：{source_url}\n"
        f"原标题：{title}\n"
        f"正文：{body_excerpt}"
    )

    try:
        response = client.chat.completions.create(
            model=llm_config.get("model", "deepseek-chat"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=min(int(llm_config.get("max_tokens", 2048)), 700),
        )
        content = _strip_json_fence(response.choices[0].message.content or "")
        result = json.loads(content)
    except Exception as exc:
        return _fallback_featured_item(item, llm_error=str(exc))

    if not isinstance(result, dict):
        return _fallback_featured_item(item, llm_error="LLM 返回格式无效")

    out_title = str(result.get("title") or title).strip() or title
    what_happened = str(result.get("what_happened") or "").strip()
    impact = str(result.get("impact") or "").strip()
    theme = str(result.get("theme") or item.get("theme") or "出货风险").strip()
    if theme not in _VALID_THEMES:
        theme = str(item.get("theme") or "出货风险")

    if len(_CJK_RE.findall(f"{what_happened}{impact}")) < 12:
        return _fallback_featured_item(item, llm_error="LLM 输出过短")

    return {
        **_candidate_payload(item),
        "title": out_title[:80],
        "what_happened": what_happened[:320],
        "impact": impact[:320],
        "theme": theme,
        "summary": what_happened,
        "llm_ok": True,
        "finalized": True,
    }


def _fallback_featured_item(item: dict, llm_error: str = "") -> dict:
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or item.get("ai_summary") or "").strip()
    return {
        **_candidate_payload(item),
        "title": title,
        "what_happened": summary[:320] if summary else title,
        "impact": "影响需结合原文人工确认。",
        "summary": summary,
        "llm_ok": False,
        "llm_error": llm_error,
        "finalized": True,
    }


def finalize_featured(
    source_urls: list[str],
    date: str | None = None,
    use_llm: bool = True,
    source: str = "manual",
) -> dict:
    weights = load_featured_weights()
    max_items = int(weights.get("max_items") or 5)
    platform_max = int(weights.get("platform_logistics_max") or 1)
    day = date or today_local()
    urls = []
    for url in source_urls:
        u = (url or "").strip()
        if u and u not in urls:
            urls.append(u)
    urls = urls[:max_items]

    with _featured_lock:
        lookup = _lookup_today_items_by_urls(urls, date=day)
        items: list[dict] = []
        missing: list[str] = []
        platform_count = 0
        for url in urls:
            raw = lookup.get(url)
            if not raw:
                missing.append(url)
                continue
            if not _passes_hard_filters(raw, weights):
                missing.append(url)
                continue
            scored = score_candidate(raw, weights)
            if scored.get("theme") == "平台物流":
                if platform_count >= platform_max:
                    missing.append(url)
                    continue
                platform_count += 1
            if use_llm:
                items.append(format_featured_item_with_llm(scored))
            else:
                items.append(_fallback_featured_item(scored))

        payload = {
            "date": day,
            "finalized": True,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "source": source if source in ("manual", "auto") else "manual",
            "items": items,
            "missing_urls": missing,
        }
        save_featured_store(payload, date=day)
        return payload


def is_manual_featured(date: str | None = None) -> bool:
    store = load_featured_store(date)
    return bool(
        store
        and store.get("finalized")
        and store.get("source") == "manual"
        and store.get("items")
    )


def auto_format_featured(date: str | None = None, *, force: bool = False) -> dict:
    """规则打分 Top5 后自动过 DeepSeek 排版。人工定稿优先，不被自动覆盖。"""
    day = date or today_local()
    store = load_featured_store(day)
    if not force and is_manual_featured(day):
        _log.info("今日精选已人工定稿，跳过自动排版 date=%s", day)
        return {
            **store,
            "skipped": True,
            "skip_reason": "manual_finalized",
        }

    max_items = int(load_featured_weights().get("max_items") or 5)
    candidates = build_featured_candidates(date=day, limit=max_items)
    urls = [
        str(row.get("source_url") or "").strip()
        for row in candidates
        if str(row.get("source_url") or "").strip()
    ]
    if not urls:
        _log.warning("自动精选无候选 date=%s", day)
        if store and store.get("items"):
            return {**store, "skipped": True, "skip_reason": "no_candidates"}
        empty = {
            "date": day,
            "finalized": False,
            "source": "auto",
            "items": [],
            "missing_urls": [],
        }
        save_featured_store(empty, date=day)
        return empty

    _log.info("自动精选排版开始 date=%s urls=%d", day, len(urls))
    result = finalize_featured(urls, date=day, use_llm=True, source="auto")
    llm_ok = sum(1 for item in (result.get("items") or []) if item.get("llm_ok"))
    _log.info(
        "自动精选排版完成 date=%s items=%d llm_ok=%d",
        day,
        len(result.get("items") or []),
        llm_ok,
    )
    return result


def get_featured_response(date: str | None = None) -> dict:
    day = date or today_local()
    store = load_featured_store(day)
    if store and store.get("finalized") and store.get("items"):
        items = store.get("items") or []
        return {
            "date": day,
            "items": items[: int(load_featured_weights().get("max_items") or 5)],
            "total": len(items),
            "finalized": True,
            "source": store.get("source") or "manual",
            "updated_at": store.get("updated_at") or store.get("finalized_at") or "",
        }

    rule_items = _rule_based_featured(day)
    return {
        "date": day,
        "items": rule_items,
        "total": len(rule_items),
        "finalized": False,
        "source": "rules",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "needs_auto_format": True,
    }
