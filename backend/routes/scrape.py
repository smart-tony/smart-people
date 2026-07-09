"""
内容采集管道 — Phase 2: 并行化 + 专用选择器 + 持久化 + 缓存

POST /api/scrape/fetch 
    管道: 读配置 → 并发抓取 → 去重 → 缓存过滤 → AI评分 → 过滤 → 持久化 → 返回候选
"""

import json
import os
import re
import hashlib
import asyncio
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from models import RawArticle, CandidateItem, FetchResult, ReportBriefing
from settings import load_llm_config, get_data_dir, require_auth

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = get_data_dir()
DRAFTS_DIR = DATA_DIR / "drafts"
CACHE_DIR = DATA_DIR / "cache"

router = APIRouter(prefix="/api/scrape", tags=["采集管道"], dependencies=[Depends(require_auth)])

PLAYWRIGHT_SOURCE_DOMAINS = {
    # 仅保留物流/跨境相关，AI科技域名已移除
    "kjdsnews.com",
    "shippingchina.com",
}
PLAYWRIGHT_ARTICLE_DOMAINS = set(PLAYWRIGHT_SOURCE_DOMAINS)
PLAYWRIGHT_CONCURRENCY = asyncio.Semaphore(2)
RECRUITMENT_RE = re.compile(
    r"招聘|求职|职位|岗位|诚聘|急招|招募|人才招聘|简历|投递|薪资|"
    r"工作机会|热门职位|加入我们|社招|校招|内推|"
    r"\b(?:hiring|job|jobs|career|careers|recruit|recruitment|resume|cv)\b",
    re.I,
)
ARTICLE_META_PREFIX_RE = re.compile(
    r"^(?:[\u4e00-\u9fffA-Za-z0-9&＋+_.·・\-]{2,30}\s*[•·]\s*)?"
    r"(?:刚刚|\d+\s*(?:秒|分钟|小时|天|周|月)前|20\d{2}[年/-]\d{1,2}[月/-]\d{1,2}(?:日)?)\s*"
    r"(?:[•·]\s*(?!(?:阅读|浏览|点击)\b)[^•·]{1,20}){0,2}\s*"
    r"(?:[•·]\s*(?:阅读|浏览|点击)\s*\d+)?\s*",
    re.I,
)


def _looks_mojibake(text: str) -> bool:
    """判断中文页面是否被错误按 utf-8 解码。"""
    if not text:
        return False
    sample = text[:4000]
    suspicious = sample.count("�") + sample.count("ƽ") + sample.count("ϰ") + sample.count("�")
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", sample))
    return suspicious >= 2 and not has_cjk


def _decode_response_text(resp: httpx.Response) -> str:
    """用 meta charset/header/常见中文编码兜底，避免政府站 GBK 页面乱码。"""
    content = resp.content or b""
    head = content[:3000].decode("ascii", errors="ignore")
    charset_match = re.search(r"charset=[\"']?\s*([A-Za-z0-9_-]+)", head, re.I)
    candidates = []
    if charset_match:
        candidates.append(charset_match.group(1))
    if resp.encoding:
        candidates.append(resp.encoding)
    candidates.extend(["utf-8", "gb18030", "gbk", "gb2312"])

    seen = set()
    decoded_options = []
    for encoding in candidates:
        normalized = encoding.lower().replace("_", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            decoded = content.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
        decoded_options.append(decoded)
        if not _looks_mojibake(decoded):
            return decoded

    return decoded_options[0] if decoded_options else resp.text

# ── task_type 别名映射 ────────────────────────────────────
# sources.config.json 的模块 key 可能与 LLM 的 VALID_TASK_TYPES 不同，
# 此映射确保前端传 logistics-daily 等模块名时能正确匹配到 prompt。
TASK_TYPE_ALIASES: dict[str, str] = {
    "logistics-daily": "cn-logistics-industry",
    "global-news": "global-logistics-risk",
    "policy-official": "global-logistics-risk",
    "crossborder-platform": "cn-logistics-industry",
    "shipping-port": "cn-logistics-industry",
    "by56-wiki": "cn-logistics-industry",
}


def resolve_task_type(task_type: str) -> str:
    """将 sources.config 中的模块名映射为 LLM prompt 对应的标准 task_type。
    pick_sources 用原始 task_type 匹配来源，analyze_with_llm 用转换后的标准名。"""
    return TASK_TYPE_ALIASES.get(task_type, task_type)


# ── 请求模型 ──────────────────────────────────────────────

class FetchRequest(BaseModel):
    task_type: str = Field(
        ..., description="任务类型: ai-weekly | logistics-daily | global-logistics-risk | cn-logistics-industry | exchange-rate | weather-alert"
    )
    source_ids: list[str] = Field(
        default_factory=list, description="指定来源 ID，空=全部启用来源"
    )
    limit: int = Field(default=5, ge=1, le=15, description="每个来源最多抓取条数")
    score_threshold: float = Field(
        default=0, ge=0, le=10, description="AI 打分阈值，低于此分的候选被过滤"
    )
    force_refresh: bool = Field(
        default=False, description="是否强制刷新，跳过缓存"
    )
    analyze_with_llm: bool = Field(
        default=False, description="是否抓取后立即调用 LLM 摘要/打分。默认关闭以节省 token"
    )
    screen_with_llm: bool = Field(
        default=True, description="是否在抓正文前用标题列表做轻量筛选。一次批量调用，低 token"
    )
    excluded_urls: list[str] = Field(
        default_factory=list, description="本轮要排除的 URL，用于换一批候选"
    )
    recency_days: int = Field(
        default=2, ge=1, le=30, description="只保留最近 N 天明确日期的内容，默认今天/昨天"
    )


class TestSourceRequest(BaseModel):
    """来源健康检查请求"""
    source_id: str
    source_url: str

class TestSourceResponse(BaseModel):
    source_id: str
    reachable: bool
    article_count: int = 0
    sample_titles: list[str] = Field(default_factory=list)
    error: Optional[str] = None


# ═══════════════════════════════════════════════════════════
# Stage ① — 读配置，选来源
# ═══════════════════════════════════════════════════════════

def load_config(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def pick_sources(task_type: str, source_ids: list[str]) -> list[dict]:
    """从 sources.config.json 选出匹配 task_type 且 enabled 的来源"""
    config = load_config("sources.config.json")
    sources = config.get("sources", {})
    if not isinstance(sources, dict):
        return []

    selected = []
    for module, group in sources.items():
        if module != task_type or not isinstance(group, list):
            continue
        for src in group:
            if isinstance(src, dict) and src.get("enabled", True):
                src_copy = dict(src)
                src_copy["module"] = module
                selected.append(src_copy)

    if source_ids:
        wanted = set(source_ids)
        selected = [s for s in selected if s.get("id") in wanted]

    return selected


# ═══════════════════════════════════════════════════════════
# Stage ② — 专用选择器：按来源域名匹配解析策略
# ═══════════════════════════════════════════════════════════

# 站点域名 → 专用提取器（仅物流/政策/跨境站点）
# 每个函数签名: (soup: BeautifulSoup, base_url: str, max_items: int) -> list[RawArticle]


def _extract_kjdsnews(soup, base_url, max_items):
    """跨境电商新闻: 文章在 .title a 中，链接匹配 /a/数字.html"""
    articles = []
    for a in soup.select(".title a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 6:
            continue
        if not re.search(r"/a/\d+\.html", href):
            continue
        if not href.startswith("https://www.kjdsnews.com/"):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _is_trade_relevant_title(title: str) -> bool:
    keywords = [
        "外贸", "贸易", "关税", "海关", "清关", "跨境", "电商", "出口", "进口",
        "口岸", "自贸", "商务部", "贸易救济", "反倾销", "反补贴", "保障措施",
        "原产地", "协定", "政策", "公告", "规则", "合规", "物流", "航运",
    ]
    return any(keyword.lower() in title.lower() for keyword in keywords)


def _extract_mofcom(soup, base_url, max_items):
    """商务部系来源：只保留商务部域名下与外贸/政策相关的链接，排除门户转载通稿。"""
    articles = []
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        host = (urlparse(href).hostname or "").lower()
        if not title or len(title) < 8:
            continue
        if "mofcom.gov.cn" not in host:
            continue
        if not _is_trade_relevant_title(title):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _clean_feed_title(title: str) -> str:
    title = " ".join((title or "").split())
    title = re.sub(r"^(?:\d+\s*(?:秒|分钟|小时|天|周|月)前|刚刚)\s*", "", title)
    title = re.sub(r"\s*分享至\s*$", "", title)
    return title.strip()


def _append_article(articles: list[RawArticle], seen_urls: set[str], title: str, href: str, max_items: int) -> bool:
    title = _clean_feed_title(title)
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not title or len(title) < 8 or href in seen_urls:
        return False
    # 过滤招聘/广告
    spam_patterns = [r'限时优惠', r'免费试用', r'扫码领取', r'广告推广']
    if RECRUITMENT_RE.search(title) or any(re.search(p, title) for p in spam_patterns):
        return False
    seen_urls.add(href)
    articles.append(RawArticle(title=title, url=href))
    return len(articles) >= max_items


def _extract_by_url_patterns(soup, base_url, max_items, allowed_patterns: list[str], blocked_patterns: list[str] | None = None):
    articles = []
    seen_urls: set[str] = set()
    blocked_patterns = blocked_patterns or []
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        href = urljoin(base_url, a["href"].strip())
        path = urlparse(href).path
        if any(re.search(pattern, href, re.I) or re.search(pattern, path, re.I) for pattern in blocked_patterns):
            continue
        if not any(re.search(pattern, href, re.I) or re.search(pattern, path, re.I) for pattern in allowed_patterns):
            continue
        if _append_article(articles, seen_urls, title, href, max_items):
            break
    return articles


def _extract_wl123(soup, base_url, max_items):
    """WL123 只取物流资讯详情页，排除公司、导航首页和工具页。"""
    return _extract_by_url_patterns(
        soup,
        base_url,
        max_items,
        allowed_patterns=[r"/wu-liu-zi-xun/.+"],
        blocked_patterns=[r"/company/", r"/sites?/", r"/jobs?/", r"/tools?/"],
    )


def _extract_chwang(soup, base_url, max_items):
    """出海网快讯：取新闻详情，清理“1秒前/分享至”等文案。"""
    return _extract_by_url_patterns(
        soup,
        base_url,
        max_items,
        allowed_patterns=[r"/news/.+"],
        blocked_patterns=[r"/news/?$", r"/service", r"/activity", r"/topic"],
    )


def _extract_cifnews(soup, base_url, max_items):
    """雨果跨境：只取文章页，排除开店/服务商品页。"""
    return _extract_by_url_patterns(
        soup,
        base_url,
        max_items,
        allowed_patterns=[r"/article/", r"/news/"],
        blocked_patterns=[r"/product/", r"/service", r"/ask/", r"/course", r"/activity"],
    )


def _extract_ship_sh(soup, base_url, max_items):
    """航运界：只取文章详情页，并保留列表上的相对发布时间。"""
    articles = []
    seen_urls: set[str] = set()
    time_re = re.compile(r"(\d+\s*(?:分钟|小时|天)前|20\d{2}年\d{1,2}月\d{1,2}日)")
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        path = urlparse(href).path.rstrip("/")
        if not re.search(r"/articles/.+", path, re.I):
            continue
        if re.search(r"mailto:|/about|/contact|/articles$", href, re.I):
            continue
        title = _clean_feed_title(a.get_text(" ", strip=True))
        if not title or len(title) < 8 or href in seen_urls:
            continue
        if RECRUITMENT_RE.search(title) or re.search(r"广告推广", title):
            continue
        article = RawArticle(title=title[:150], url=href)
        nearby_text = ""
        parent = a
        for _ in range(3):
            parent = parent.parent if parent else None
            if not parent:
                break
            nearby_text = parent.get_text(" ", strip=True)
            if time_re.search(nearby_text):
                break
        time_match = time_re.search(nearby_text)
        if time_match:
            article.content_snippet = f"[发布日期: {time_match.group(1)}]"
        articles.append(article)
        seen_urls.add(href)
        if len(articles) >= max_items:
            break
    return articles


def _extract_egainnews(soup, base_url, max_items):
    """蓝海亿观网：只取 /article/，排除服务/店铺/导航页。"""
    return _extract_by_url_patterns(
        soup,
        base_url,
        max_items,
        allowed_patterns=[r"/article/\d+"],
        blocked_patterns=[r"/service", r"/shop", r"/store", r"/login", r"/register",
                          r"/about", r"/contact", r"/tag/", r"/category/"],
    )


def _extract_5688(soup, base_url, max_items):
    """物流巴巴：取新闻详情页，避免频道首页。"""
    return _extract_by_url_patterns(
        soup,
        base_url,
        max_items,
        allowed_patterns=[r"/news/.+", r"/news_\d+", r"/article/"],
        blocked_patterns=[r"/news/?$", r"/company", r"/product"],
    )


def _extract_aircargoweek(soup, base_url, max_items):
    """Air Cargo Week：英文空运新闻，每次只取少量详情页。"""
    return _extract_by_url_patterns(
        soup,
        base_url,
        min(max_items, 7),
        allowed_patterns=[r"aircargoweek\.com/.+"],
        blocked_patterns=[
            r"/news/?$", r"/events?", r"/jobs?", r"/subscribe", r"/advertis",
            r"/contact", r"/about", r"linkedin\.com", r"facebook\.com",
            r"twitter\.com", r"x\.com", r"instagram\.com", r"youtube\.com",
        ],
    )


def _extract_stattimes(soup, base_url, max_items):
    """STAT Times：英文航空货运/物流新闻，每次只取少量详情页。"""
    return _extract_by_url_patterns(
        soup,
        base_url,
        min(max_items, 7),
        allowed_patterns=[r"/news/.+", r"/air-cargo/.+", r"/aviation/.+", r"/logistics/.+"],
        blocked_patterns=[r"/events?", r"/jobs?", r"/subscribe", r"/advertis", r"/contact", r"/about", r"utm_"],
    )


def _extract_generic(soup, base_url, max_items):
    """通用提取：所有 <a> 标签，过滤噪音"""
    articles = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        parsed = urlparse(href)

        # 过滤噪音
        if parsed.scheme not in {"http", "https"}:
            continue
        if not title or len(title) < 10:
            continue
        if href in seen_urls:
            continue
        if re.search(r"(login|logout|register|javascript:|void\(\)|#)$", href, re.I):
            continue
        path_part = urlparse(href).path.strip("/")
        if path_part and "/" not in path_part and "." not in path_part:
            continue

        seen_urls.add(href)
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break

    return articles


# 域名 → 提取函数映射

def _extract_by56(soup, base_url, max_items):
    """百运百科：只取 /news/数字.html 文章页"""
    return _extract_by_url_patterns(
        soup,
        base_url,
        max_items,
        allowed_patterns=[r"/news/\d+\.html"],
        blocked_patterns=[r"/newsIndex", r"/tag/", r"/category/", r"/login", r"/register"],
    )



def _extract_chwang_daily(soup, base_url, max_items):
    """出海网日报：直接从日报页提取标题+摘要，不爬详情页"""
    articles = []
    seen = set()
    
    # 日报页的文章结构：heading 里是标题，后面有 "查看全文" 链接
    for heading in soup.find_all(['h2', 'h3']):
        link = heading.find('a')
        if not link:
            continue
        title = link.get_text(' ', strip=True)
        href = link.get('href', '')
        if not title or len(title) < 10 or not href:
            continue
        # 清洗序号
        title = re.sub(r'^\d+\s+', '', title).strip()
        if href in seen or len(title) < 10:
            continue
        seen.add(href)
        articles.append(RawArticle(title=title[:120], url=urljoin(base_url, href)))
        if len(articles) >= max_items:
            break
    
    if not articles:
        # fallback: find all 查看全文 links
        for a in soup.find_all('a', string=re.compile(r'查看全文')):
            href = a.get('href', '')
            parent = a.find_parent(['div', 'section'])
            if parent:
                h = parent.find(['h2', 'h3'])
                if h:
                    title = re.sub(r'^\d+\s+', '', h.get_text(' ', strip=True)).strip()
                    if title and len(title) > 10:
                        articles.append(RawArticle(title=title[:120], url=urljoin(base_url, href)))
    
    return articles[:max_items]



def _extract_chwang_daily(soup, base_url, max_items):
    """出海网日报：从日报页提取 h2 标题+描述文本"""
    articles = []
    seen = set()
    
    # 找所有 class=chw-dailyGroupItem__title 的 h2
    for h2 in soup.find_all('h2', class_='chw-dailyGroupItem__title'):
        # h2 被 <a href="/news/..."> 包裹
        parent_a = h2.find_parent('a')
        if not parent_a:
            continue
        href = parent_a.get('href', '')
        if not href or not ('/news/' in href or '/article/' in href):
            continue
        
        # 去掉 <span class="num">01</span>
        num_span = h2.find('span', class_='num')
        if num_span:
            num_span.decompose()
        title = h2.get_text(' ', strip=True)
        
        if len(title) < 10 or href in seen:
            continue
        seen.add(href)
        
        # 找描述文本：父级 div 里的 chw-dailyGroupItem__description
        item_div = parent_a.find_parent('div', class_='chw-dailyGroupItem')
        description = ''
        if item_div:
            desc_div = item_div.find('div', class_='chw-dailyGroupItem__description')
            if desc_div:
                # 去掉 "查看全文" 链接
                more_link = desc_div.find('a', class_='more')
                if more_link:
                    more_link.decompose()
                description = desc_div.get_text(' ', strip=True)[:2000]
        
        article = RawArticle(title=title[:150], url=urljoin(base_url, href))
        if description:
            article.content_snippet = description
        articles.append(article)
        
        if len(articles) >= max_items:
            break
    
    return articles




def _extract_wto(soup, base_url, max_items):
    """WTO新闻页：从 h3 标题 + News item 链接提取"""
    articles = []
    seen = set()
    
    # 策略1：找 "News item" 链接，往上找标题
    for link in soup.find_all('a', string=lambda s: s and 'News item' in s):
        if len(articles) >= max_items:
            break
        href = link.get('href', '')
        if not href or '/news_e/' not in href:
            continue
        
        # 找到父级块，提取 h3 标题和 p 摘要
        parent = link.find_parent(['div', 'li', 'section', 'article'])
        if not parent:
            parent = link.find_previous(['div', 'section'])
        if not parent:
            continue
        
        h3 = parent.find('h3')
        title = h3.get_text(' ', strip=True) if h3 else ''
        
        if not title or len(title) < 10:
            # 试试 h1
            h1 = parent.find('h1')
            title = h1.get_text(' ', strip=True) if h1 else ''
        
        if not title or len(title) < 10 or href in seen:
            continue
        
        seen.add(href)
        
        # 提取摘要
        p = parent.find('p')
        summary = p.get_text(' ', strip=True)[:2000] if p else ''
        
        article = RawArticle(title=title[:150], url=urljoin(base_url, href))
        if summary:
            article.content_snippet = summary
        articles.append(article)
    
    # 策略2：如果没找到，用 h3 链接兜底
    if not articles:
        for h3 in soup.find_all('h3'):
            link = h3.find('a')
            if not link:
                continue
            href = link.get('href', '')
            if '/news_e/' not in href or href in seen:
                continue
            title = h3.get_text(' ', strip=True)
            if len(title) < 10:
                continue
            seen.add(href)
            articles.append(RawArticle(title=title[:150], url=urljoin(base_url, href)))
            if len(articles) >= max_items:
                break
    
    return articles


def _extract_ebrun(soup, base_url, max_items):
    """亿邦动力：跨境电商板块，跳过"最新"tab，各 sub-tab 取最新5条"""
    articles = []
    seen = set()

    # 找到"跨境电商" tab-content div
    tab_content = None
    for tc in soup.find_all('div', class_='tab-content'):
        second_tab = tc.find('div', class_='second-tab-box')
        if not second_tab:
            continue
        labels = [a.get_text(strip=True) for a in second_tab.find_all('a')]
        if '亚马逊' in labels or 'TikTok' in labels or 'Temu' in labels:
            tab_content = tc
            break

    if not tab_content:
        # Fallback: 直接找 second-tab-content
        stc = soup.find('div', class_='second-tab-content')
        if stc:
            tab_content = stc.find_parent('div', class_='tab-content')

    if not tab_content:
        return articles

    # 获取 sub-tab 标签名（用于跳过"最新"，并在标题前加标签）
    second_tab_box = tab_content.find('div', class_='second-tab-box')
    sub_tab_labels = []
    if second_tab_box:
        for a in second_tab_box.find_all('a'):
            label = a.get_text(strip=True)
            sub_tab_labels.append(label)

    # 获取各 sub-tab 的内容区块
    second_content = tab_content.find('div', class_='second-tab-content')
    if not second_content:
        return articles

    each_parts = second_content.find_all('div', class_='second-each-part')

    for i, part in enumerate(each_parts):
        # 定位 sub-tab 标签
        label = sub_tab_labels[i] if i < len(sub_tab_labels) else ''

        # 跳过"最新"
        if '最新' in label:
            continue

        part_count = 0
        for item in part.find_all('section', class_='news-item'):
            if part_count >= 5:
                break

            # 取 URL（优先 data-dmp-url，兜底 .info .title a）
            href = item.get('data-dmp-url', '')
            if not href:
                info = item.find('div', class_='info')
                if info:
                    title_p = info.find('p', class_='title')
                    if title_p:
                        link_a = title_p.find('a')
                        if link_a:
                            href = link_a.get('href', '')

            # 校验 URL 格式
            if not href or not re.search(r'/\d{8}/\d+\.shtml', href):
                continue
            if href in seen:
                continue

            # 取标题
            title = ''
            info = item.find('div', class_='info')
            if info:
                title_p = info.find('p', class_='title')
                if title_p:
                    title_a = title_p.find('a')
                    if title_a:
                        title = title_a.get_text(' ', strip=True)
            if not title or len(title) < 8:
                continue

            # 取摘要
            summary = ''
            desc_el = item.find('p', class_='desc')
            if desc_el:
                summary = desc_el.get_text(' ', strip=True)

            # 取日期
            date_str = ''
            date_el = item.find('p', class_='date')
            if date_el:
                date_str = date_el.get_text(' ', strip=True)

            # 加子分类前缀
            display_title = f'[{label}] {title}' if label else title

            article = RawArticle(
                title=display_title[:150],
                url=urljoin(base_url, href),
            )
            if summary:
                article.content_snippet = summary[:2000]
            if date_str:
                # 附加日期到摘要尾部，供 LLM 判断时效
                if article.content_snippet:
                    article.content_snippet += f'\n[发布日期: {date_str}]'
                else:
                    article.content_snippet = f'[发布日期: {date_str}]'

            articles.append(article)
            seen.add(href)
            part_count += 1

    return articles


def _extract_mjzj(soup, base_url, max_items):
    """卖家之家：首页"全部文章"前6条"""
    articles = []
    seen = set()

    for item in soup.find_all('div', class_='article-wrap'):
        if len(articles) >= 6:
            break

        link = item.find('a', class_='article-a-wrap')
        if not link:
            continue
        href = link.get('href', '')
        if not href or '/article/' not in href:
            continue
        if href.startswith('//'):
            href = 'https:' + href
        if href in seen:
            continue

        title_el = item.find('p', class_='article-wrap-title')
        if not title_el:
            continue
        title = title_el.get_text(' ', strip=True)
        if not title or len(title) < 6:
            continue

        summary_el = item.find('p', class_='article-wrap-summary')
        summary = summary_el.get_text(' ', strip=True) if summary_el else ''

        date_str = ''
        time_el = item.find('p', class_='article-wrap-b-time')
        if time_el:
            span = time_el.find('span')
            if span:
                date_str = span.get_text(strip=True)

        article = RawArticle(title=title[:150], url=urljoin(base_url, href))
        if summary:
            article.content_snippet = summary[:2000]
        if date_str:
            if article.content_snippet:
                article.content_snippet += f'\n[发布日期: {date_str}]'
            else:
                article.content_snippet = f'[发布日期: {date_str}]'

        articles.append(article)
        seen.add(href)

    return articles


def _extract_shippingchina_medium_news(soup, base_url, max_items):
    """国际海运网 媒体平台新闻列表：Playwright 渲染后提取 /medium/news/detail/ 文章。
    列表页每个 <a> 包含标题+摘要，管道后续会逐条点进详情页取全文。
    """
    articles = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/medium/news/detail/id/" not in href:
            continue
        if href in seen:
            continue

        full_text = a.get_text(" ", strip=True)
        if len(full_text) < 15:
            continue

        # 拆分标题和摘要：以「来源：」为分界
        title = full_text
        summary = ""
        source_match = re.search(r"(来源[：:]\s*\S+)", full_text)
        if source_match:
            split_pos = source_match.start()
            title = full_text[:split_pos].strip()
            summary = full_text[split_pos:].strip()

        # 标题太短跳过
        if len(title) < 10:
            title = full_text[:80]

        article = RawArticle(
            title=title[:150],
            url=urljoin(base_url, href),
        )
        if summary:
            article.content_snippet = summary[:2000]

        articles.append(article)
        seen.add(href)
        if len(articles) >= max_items:
            break

    return articles


_EXTRACTORS = {
    # 仅保留物流/政策/跨境源，AI科技提取器已移除
    "kjdsnews.com": _extract_kjdsnews,
    "mofcom.gov.cn": _extract_mofcom,
    "wl123.com": _extract_wl123,
    "chwang.com": _extract_chwang,
    "cifnews.com": _extract_cifnews,
    "ship.sh": _extract_ship_sh,
    "egainnews.com": _extract_egainnews,
    "wto.org": _extract_wto,
    "by56.com": _extract_by56,
    "5688.cn": _extract_5688,
    "ebrun.com": _extract_ebrun,
    "mjzj.com": _extract_mjzj,
    "shippingchina.com": _extract_shippingchina_medium_news,
    "aircargoweek.com": _extract_aircargoweek,
    "stattimes.com": _extract_stattimes,
}


def _get_extractor(url: str):
    """根据 URL 域名匹配专用提取器，无匹配则返回通用提取器"""
    host = urlparse(url).hostname or ""
    path = urlparse(url).path or ""
    # chwang.com/daily 用日报提取器
    if "chwang.com" in host and "/daily" in path:
        return _extract_chwang_daily
    for domain, extractor in _EXTRACTORS.items():
        if domain in host:
            return extractor
    return _extract_generic


def _host_matches(url: str, domains: set[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(domain in host for domain in domains)


def _render_with_playwright_sync(url: str, wait_ms: int = 1200) -> tuple[str, str]:
    """使用 Playwright sync API 渲染页面，返回 (html, final_url)。
    
    改用 sync API + 独立线程执行，避开 async API 的 greenlet 线程冲突。
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("未安装 playwright，请先安装依赖") from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36"
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="commit", timeout=20000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(wait_ms)
            return page.content(), page.url
        finally:
            context.close()
            browser.close()


async def _render_with_playwright(url: str, wait_ms: int = 1200) -> tuple[str, str]:
    """兼容旧调用方：在独立线程中运行 sync Playwright，避免 greenlet 冲突。"""
    from fastapi.concurrency import run_in_threadpool
    return await run_in_threadpool(_render_with_playwright_sync, url, wait_ms)


def _extract_articles_from_html(src: dict, html: str, base_url: str, limit: int) -> list[RawArticle]:
    soup = BeautifulSoup(html, "html.parser")
    extractor = _get_extractor(src.get("url", base_url))
    articles = extractor(soup, base_url, limit)
    for a in articles:
        a.source_id = src.get("id", "")
        a.source_name = src.get("name", src.get("id", "?"))
        a.module = src.get("module", "")
    return articles


async def _fetch_one_source_playwright(src: dict, limit: int) -> tuple[list[RawArticle], list[str]]:
    """使用 Playwright 抓取单个来源列表页。"""
    src_name = src.get("name", src.get("id", "?"))
    src_url = src.get("url", "")
    async with PLAYWRIGHT_CONCURRENCY:
        try:
            html, final_url = await _render_with_playwright(src_url)
            articles = _extract_articles_from_html(src, html, final_url, limit)
            if not articles:
                return [], [f"{src_name}: Playwright 渲染后仍未提取到文章"]
            return articles, []
        except Exception as e:
            return [], [f"{src_name}: Playwright 渲染失败: {e}"]


# ═══════════════════════════════════════════════════════════
# Stage ② — 并发抓取原始文章
# ═══════════════════════════════════════════════════════════

async def _fetch_one_source(src: dict, limit: int, client: httpx.AsyncClient) -> tuple[list[RawArticle], list[str]]:
    """抓取单个来源，返回 (articles, errors)"""
    src_name = src.get("name", src.get("id", "?"))
    src_url = src.get("url", "")
    if not src_url:
        return [], []

    try:
        resp = await client.get(
            src_url,
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        )
        resp.raise_for_status()
        articles = _extract_articles_from_html(src, _decode_response_text(resp), str(resp.url), limit)

        if not articles:
            if _host_matches(src_url, PLAYWRIGHT_SOURCE_DOMAINS):
                pw_articles, pw_errors = await _fetch_one_source_playwright(src, limit)
                if pw_articles:
                    return pw_articles, [f"{src_name}: httpx 未提取到文章，已自动切换 Playwright"]
                return [], pw_errors or [f"{src_name}: 未提取到文章（页面结构可能已变化）"]
            return [], [f"{src_name}: 未提取到文章（页面结构可能已变化）"]

        return articles, []

    except httpx.TimeoutException:
        if _host_matches(src_url, PLAYWRIGHT_SOURCE_DOMAINS):
            pw_articles, pw_errors = await _fetch_one_source_playwright(src, limit)
            if pw_articles:
                return pw_articles, [f"{src_name}: httpx 请求超时，已切换 Playwright"]
            return [], pw_errors or [f"{src_name}: 请求超时"]
        return [], [f"{src_name}: 请求超时"]
    except httpx.HTTPStatusError as e:
        if _host_matches(src_url, PLAYWRIGHT_SOURCE_DOMAINS):
            pw_articles, pw_errors = await _fetch_one_source_playwright(src, limit)
            if pw_articles:
                return pw_articles, [f"{src_name}: HTTP {e.response.status_code}，已切换 Playwright"]
            return [], pw_errors or [f"{src_name}: HTTP {e.response.status_code}"]
        return [], [f"{src_name}: HTTP {e.response.status_code}"]
    except Exception as e:
        if _host_matches(src_url, PLAYWRIGHT_SOURCE_DOMAINS):
            pw_articles, pw_errors = await _fetch_one_source_playwright(src, limit)
            if pw_articles:
                return pw_articles, [f"{src_name}: httpx 抓取异常，已切换 Playwright"]
            return [], pw_errors or [f"{src_name}: {e}"]
        return [], [f"{src_name}: {e}"]


async def fetch_raw_articles(sources: list[dict], limit: int) -> tuple[list[RawArticle], list[str]]:
    """并发抓取所有来源 — 每个来源最多 limit 篇"""
    all_articles = []
    all_errors = []

    async with httpx.AsyncClient() as client:
        tasks = [_fetch_one_source(src, limit, client) for src in sources]
        results = await asyncio.gather(*tasks)

    for articles, errors in results:
        all_articles.extend(articles)
        all_errors.extend(errors)

    return all_articles, all_errors


# ═══════════════════════════════════════════════════════════
# Stage ②.5 — 抓取文章正文（并发版）
# ═══════════════════════════════════════════════════════════

def extract_body_text(html: str, title: str = "") -> str:
    """从文章页 HTML 提取正文文本（智能选择器 + 密集文本兜底）"""
    soup = BeautifulSoup(html, "html.parser")

    # 移除干扰元素
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                      "noscript", "iframe", "form"]):
        tag.decompose()

    # 移除常见非正文区域（导航、侧栏、评论区、推荐阅读等）
    noise_selectors = [
        ".nav", ".navbar", ".navigation", ".menu", ".sidebar", ".side-bar",
        ".footer", ".header", ".comment", ".comments", ".recommend",
        ".related", ".share", ".breadcrumb", ".pagination", ".ad",
        ".topbar", ".top-bar", ".toolbar", ".copyright",
    ]
    for sel in noise_selectors:
        for tag in soup.select(sel):
            tag.decompose()

    # —— 策略 1：精确匹配常见文章容器 ——
    article_selectors = [
        "article",
        ".article-content", ".article-body", ".article-detail", ".article-text",
        ".post-content", ".post-body", ".entry-content",
        ".content", ".main-content", ".detail-content", ".news-content",
        ".news-detail", ".news-text", ".news-body",
        "#content", "#article", "#main-content",
        ".txt", ".text", ".description",
        '[class*="article"]', '[class*="content"]', '[class*="detail"]',
    ]
    for sel in article_selectors:
        el = soup.select_one(sel)
        if el:
            text = _clean_common_article_text(el.get_text(" ", strip=True), title)
            # 过滤太短的（可能是空的 article 标签）
            if len(text) >= 100:
                return text[:3000] if len(text) > 3000 else text

    # —— 策略 2：找文本密度最大的块级元素 ——
    candidates = []
    for el in soup.find_all(["div", "section", "main"]):
        text = _clean_common_article_text(el.get_text(" ", strip=True), title)
        # 忽略太短和太长的（太长的可能是整个页面）
        if 200 <= len(text) <= 10000:
            # 计算"实质文本密度"：文字长度 / 标签数量（标签越多越可能是导航）
            tag_count = len(el.find_all()) + 1
            density = len(text) / tag_count
            candidates.append((density, text))

    if candidates:
        # 取密度最高的
        candidates.sort(key=lambda x: -x[0])
        text = candidates[0][1]
        return text[:3000] if len(text) > 3000 else text

    # —— 策略 3：兜底 ——
    text = _clean_common_article_text(soup.get_text(" ", strip=True), title)
    return text[:3000] if len(text) > 3000 else text


def _clean_common_article_text(text: str, title: str = "") -> str:
    """清理资讯站正文中的站点模板、客服、公众号、返回顶部等噪音。"""
    text = re.sub(r"^\ufeff+", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = ARTICLE_META_PREFIX_RE.sub("", text).strip()
    if title:
        text = re.sub(rf"^{re.escape(title)}(?:_跨境知道)?\s*", "", text)

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
    text = ARTICLE_META_PREFIX_RE.sub("", text).strip()
    return re.sub(r"\s{2,}", " ", text).strip(" _-｜|")


def extract_ikjzd_body_text(html: str, title: str = "") -> str:
    """跨境知道文章页：正文在 .articlecontent，通用提取容易误命中客服浮层。"""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for sel in [".articlecontent", ".mainbox.shownews .articlecontent", ".shownews .articlecontent"]:
        for el in soup.select(sel):
            text = _clean_common_article_text(el.get_text(" ", strip=True), title)
            if len(text) >= 30:
                candidates.append(text)

    if candidates:
        return max(candidates, key=len)[:3000]

    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        text = _clean_common_article_text(meta.get("content", ""), title)
        if len(text) >= 30:
            return text[:3000]

    return extract_body_text(html, title)


def _clean_by56_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    noise_patterns = [
        r"20\d{2}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{1,2}:\d{1,2}\s+更新",
        r"\d+\s*浏览",
        r"作者[:：]\s*百运网",
        r"货物所在地\s*目的国家\s*货物信息\s*KG\s*获取报价",
        r"获取报价",
        r"上一篇[:：]?.*",
        r"下一篇[:：]?.*",
        r"相关阅读.*",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, " ", text, flags=re.I)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def summarize_by56_body_text(text: str, title: str = "") -> str:
    text = _clean_by56_text(text)
    if title:
        escaped_title = re.escape(title.strip())
        text = re.sub(rf"^{escaped_title}\s*", "", text)
    if not text:
        return ""
    sentences = re.split(r"(?<=[。！？；])\s*", text)
    picked = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 16:
            continue
        if RECRUITMENT_RE.search(sentence):
            continue
        picked.append(sentence)
        if len("".join(picked)) >= 120 or len(picked) >= 3:
            break
    summary = "".join(picked) if picked else text[:180]
    summary = summary.strip()
    if len(summary) > 180:
        cut = summary[:180].rfind("。")
        if cut < 60:
            cut = summary[:180].rfind("，")
        if cut < 60:
            cut = 178
        summary = summary[:cut + 1].rstrip("，。； ") + "…"
    return summary


def extract_by56_body_text(html: str, title: str = "") -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript"]):
        tag.decompose()
    for sel in [
        ".breadcrumb", ".share", ".related", ".recommend", ".comment", ".footer",
        ".header", ".nav", ".sidebar", ".page", ".pagination", ".ad",
        '[class*="quote"]', '[class*="form"]', '[class*="author"]', '[class*="meta"]',
    ]:
        for tag in soup.select(sel):
            tag.decompose()
    selectors = [
        ".newsDetail", ".news-detail", ".article-content", ".article-detail",
        ".detail-content", ".content", ".main-content", "article",
    ]
    candidates = []
    for sel in selectors:
        for el in soup.select(sel):
            text = _clean_by56_text(el.get_text(" ", strip=True))
            if len(text) >= 80:
                candidates.append(text)
    if not candidates:
        for el in soup.find_all(["div", "section", "main"]):
            text = _clean_by56_text(el.get_text(" ", strip=True))
            if 120 <= len(text) <= 8000:
                tag_count = len(el.find_all()) + 1
                candidates.append((len(text) / tag_count, text))
        if candidates and isinstance(candidates[0], tuple):
            candidates.sort(key=lambda x: -x[0])
            text = candidates[0][1]
        else:
            text = _clean_by56_text(soup.get_text(" ", strip=True))
    else:
        text = max(candidates, key=len)
    return summarize_by56_body_text(text, title)


def _extract_og_image(html: str) -> str:
    """从 HTML 中提取 og:image 或 twitter:image 作为封面图"""
    soup = BeautifulSoup(html, "html.parser")
    for prop in ("og:image", "twitter:image"):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content", "").startswith("http"):
            return tag["content"]
    return ""


def _trafilatura_extract(html: str) -> str:
    """用 trafilatura 提取正文（同步函数，由线程池调用）"""
    return trafilatura.extract(html, include_links=False, include_images=False) or ""


def _trafilatura_metadata(html: str) -> dict:
    """用 trafilatura 提取元数据"""
    try:
        raw = trafilatura.extract(html, output_format="json", with_metadata=True)
        if raw:
            import json as _json
            return _json.loads(raw)
    except Exception:
        pass
    return {}


def _fallback_extract_body(html: str, article) -> str:
    """trafilatura 失败时的回退方案 — 保留原有选择器逻辑"""
    if _host_matches(article.url, {"by56.com"}):
        return extract_by56_body_text(html, article.title)
    elif _host_matches(article.url, {"ikjzd.com"}):
        return extract_ikjzd_body_text(html, article.title)
    else:
        return extract_body_text(html, article.title)


async def _enrich_one(article: RawArticle, client: httpx.AsyncClient) -> RawArticle:
    """并发抓取单篇文章正文和封面图 — trafilatura 优先，回退手写选择器"""
    try:
        resp = await client.get(
            article.url,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        )
        resp.raise_for_status()
        html_text = _decode_response_text(resp)

        # trafilatura 提取正文（同步函数，跑在线程池避免阻塞事件循环）
        loop = asyncio.get_event_loop()
        try:
            extracted = await loop.run_in_executor(None, _trafilatura_extract, html_text)
            if extracted and len(extracted) >= 80:
                article.content_snippet = extracted
                # 尝试提取元数据补充日期
                meta = await loop.run_in_executor(None, _trafilatura_metadata, html_text)
                if meta.get("date"):
                    article.content_snippet = f"[发布日期: {meta['date']}]\n\n{article.content_snippet}"
            else:
                article.content_snippet = _fallback_extract_body(html_text, article)
        except Exception:
            article.content_snippet = _fallback_extract_body(html_text, article)

        if not article.image:
            article.image = _extract_og_image(html_text)

        # 保存原始HTML用于后续LLM清洗
        article._html_text = html_text

    except Exception:
        if _host_matches(article.url, PLAYWRIGHT_ARTICLE_DOMAINS):
            try:
                html, _ = await _render_with_playwright(article.url, 800)
                loop = asyncio.get_event_loop()
                try:
                    extracted = await loop.run_in_executor(None, _trafilatura_extract, html)
                    if extracted and len(extracted) >= 80:
                        article.content_snippet = extracted
                    else:
                        article.content_snippet = _fallback_extract_body(html, article)
                except Exception:
                    article.content_snippet = _fallback_extract_body(html, article)
                if not article.image:
                    article.image = _extract_og_image(html)
                article._html_text = html
            except Exception:
                pass
    return article


async def enrich_article_content(articles: list[RawArticle]) -> list[RawArticle]:
    """并发抓取所有文章正文"""
    if not articles:
        return articles
    async with httpx.AsyncClient() as client:
        tasks = [_enrich_one(a, client) for a in articles]
        return list(await asyncio.gather(*tasks))


# ═══════════════════════════════════════════════════════════
# Stage ②.5.5 — ContentAgent：可选 LLM 正文清洗
# ═══════════════════════════════════════════════════════════

CLEAN_WITH_LLM_TASKS = {
    "logistics-daily",
    "global-news",
    "policy-official",
    "crossborder-platform",
    "shipping-port",
    "cn-logistics-industry",
    "global-logistics-risk",
}

CONTENT_NOISE_RE = re.compile(
    r"客服|加我微信|公众号|微信扫一扫|扫码|返回顶部|上一篇|下一篇|相关阅读|相关推荐|"
    r"版权所有|版权声明|联系我们|客服热线|当前位置|首页\s*>|分享到|点赞|在看|收藏|"
    r"阅读\s*\d+|浏览\s*\d+|来源[:：]|作者[:：]|编辑[:：]|责任编辑|"
    r"\b(?:copyright|all rights reserved|subscribe|newsletter|advertisement)\b",
    re.I,
)

_CLEAN_SYSTEM_PROMPT = """你是内容清洗与排版助手。从网页抓取的原始文本中提取纯净正文并做基础排版。

核心原则：
- 只保留文章正文，去掉导航、客服、公众号、广告、版权、推荐阅读、上一篇/下一篇等非正文噪音
- 不改写、不总结、不编造原文没有的信息
- 如果无法判断正文，请返回原始文本中最像正文的连续段落，不要补充背景信息
- 只输出清洗后的正文，不要输出说明、标题、引号或任何额外前后缀"""

_CLEAN_USER_TEMPLATE = """请清洗以下网页抓取内容，去掉噪音，提取纯正文并排版。

噪音示例（类似这些请全部删除）：
- "壹流融媒 • 7分钟前 • 物流资讯 • 阅读 0"
- "来源：XX公众号 作者：XX 编辑：XX"
- "首页 > 新闻 > 行业动态"
- "扫码加入粉丝群 | 客服热线：400-XXX | ©2024 XX网 版权所有"
- "上一篇：XXX | 下一篇：XXX | 相关阅读：XXX"
- "分享到朋友圈 点赞 在看 收藏"

排版要求：
1. 段落间空一行
2. 被截断的中文短句合并到下一句
3. 连续空行压缩为一个
4. 中英文之间加空格："Delta 航空"、"GDP 增长"
5. 数字与中文之间加空格："约 30 万吨"、"增长 15%"

---开始清洗---

{raw_text}"""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _is_valid_cleaned_content(cleaned: str) -> bool:
    text = (cleaned or "").strip()
    if len(text) < 50:
        return False
    if re.search(r"我无法|作为(?:一个)?AI|以下是|清洗后|抱歉|无法判断", text):
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", text):
        return False
    if not re.search(r"[。！？.!?]", text):
        return False
    return True


def _should_clean_content_with_llm(article: RawArticle, task_type: str) -> bool:
    if not _env_flag("CLEAN_WITH_LLM"):
        return False
    if task_type == "ai-weekly" or article.source_id == "by56-wiki":
        return False
    if task_type not in CLEAN_WITH_LLM_TASKS:
        return False
    body = (article.content_snippet or "").strip()
    if len(body) < 80:
        return False
    return bool(CONTENT_NOISE_RE.search(body))


def _clean_content_with_llm(raw_text: str) -> str:
    """ContentAgent: LLM 提取正文 + 排版。失败时降级返回原文。"""
    if not raw_text or len(raw_text) < 80:
        return raw_text
    try:
        from routes.llm import get_llm_client

        client = get_llm_client()
        config = load_llm_config()
        if not client:
            return raw_text

        response = client.chat.completions.create(
            model=config.get("model", "deepseek-chat"),
            messages=[
                {"role": "system", "content": _CLEAN_SYSTEM_PROMPT},
                {"role": "user", "content": _CLEAN_USER_TEMPLATE.format(raw_text=raw_text[:3000])},
            ],
            temperature=0,
            max_tokens=1500,
            timeout=10,
        )
        cleaned = response.choices[0].message.content.strip()
        if not _is_valid_cleaned_content(cleaned):
            return raw_text
        return cleaned
    except Exception:
        return raw_text


def clean_article_contents_with_llm(articles: list[RawArticle], task_type: str) -> tuple[list[RawArticle], int]:
    """仅对物流/政策中疑似混入网页模板的正文做 LLM 清洗。"""
    cleaned_count = 0
    for article in articles:
        if not _should_clean_content_with_llm(article, task_type):
            continue
        before = article.content_snippet or ""
        after = _clean_content_with_llm(before)
        if after != before:
            article.content_snippet = after
            cleaned_count += 1
    return articles, cleaned_count


# ═══════════════════════════════════════════════════════════
# Stage ②.6 — 缓存：跳过已处理的 URL（7 天自动过期）
# ═══════════════════════════════════════════════════════════

CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 天


def _cache_key(url: str) -> str:
    """为 URL 生成缓存键"""
    return hashlib.md5(url.encode()).hexdigest()


def _load_cache() -> set[str]:
    """加载未过期的缓存键。格式：md5hex|timestamp（旧行无时间戳视为永不过期）。"""
    cache_set = set()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "processed_urls.txt"
    if not cache_file.exists():
        return cache_set
    now = time.time()
    with open(cache_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                md5hex, ts_str = line.rsplit("|", 1)
                try:
                    ts = float(ts_str)
                    if now - ts > CACHE_TTL_SECONDS:
                        continue  # 过期跳过
                except ValueError:
                    pass  # 时间戳解析失败，保留
                cache_set.add(md5hex)
            else:
                # 旧格式（无时间戳），视为永不过期
                cache_set.add(line)
    return cache_set


def _save_to_cache(urls: list[str]):
    """将 URL 追加到缓存（带时间戳），同时清理过期条目。

    并发安全：追加用 O_APPEND 模式（POSIX 保证原子），重写用 tmp + os.replace 原子替换。
    """
    import os as _os
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "processed_urls.txt"
    new_keys = {_cache_key(u) for u in urls}
    existing = _load_cache()
    to_add = new_keys - existing
    if not to_add:
        return

    now_ts = time.time()
    # 追加新条目（带时间戳）。O_APPEND 在同一文件系统上原子，多 worker 并发安全。
    with open(cache_file, "a") as f:
        for key in to_add:
            f.write(f"{key}|{now_ts}\n")

    # 定期清理：文件超过 500 行时重写过期条目
    line_count = 0
    try:
        with open(cache_file, "r") as f:
            for _ in f:
                line_count += 1
    except Exception:
        return

    if line_count < 500:
        return

    # 重写文件，去掉过期条目（原子替换，避免写到一半损坏）
    kept_lines = []
    now = time.time()
    with open(cache_file, "r") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            if "|" in stripped:
                _, ts_str = stripped.rsplit("|", 1)
                try:
                    if now - float(ts_str) > CACHE_TTL_SECONDS:
                        continue
                except ValueError:
                    pass
            kept_lines.append(stripped)

    tmp_path = cache_file.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for line in kept_lines:
            f.write(line + "\n")
    _os.replace(tmp_path, cache_file)


def filter_cached(articles: list[RawArticle]) -> tuple[list[RawArticle], int]:
    """过滤已缓存的文章，返回 (新文章, 缓存命中数)"""
    cache = _load_cache()
    new_articles = []
    cached_count = 0
    for a in articles:
        if _cache_key(a.url) in cache:
            cached_count += 1
        else:
            new_articles.append(a)
    return new_articles, cached_count


# ═══════════════════════════════════════════════════════════
# Stage ③ — URL 去重
# ═══════════════════════════════════════════════════════════

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def deduplicate_by_url(articles: list[RawArticle]) -> list[RawArticle]:
    groups: dict[str, list[RawArticle]] = {}
    for a in articles:
        key = normalize_url(a.url)
        groups.setdefault(key, []).append(a)

    merged = []
    for key, group in groups.items():
        primary = max(group, key=lambda x: len(x.title))
        if len(group) > 1:
            other_sources = sorted({
                a.source_name for a in group
                if a.source_name and a.source_name != primary.source_name
            })
            if other_sources:
                primary.source_name = f"{primary.source_name} (同步自 {', '.join(other_sources)})"
        merged.append(primary)

    return merged


def _extract_article_date(article: RawArticle):
    text = " ".join([
        article.title or "",
        article.content_snippet or "",
        article.url or "",
    ])

    patterns = [
        # 标准日期
        r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
        r"(20\d{2})(\d{2})(\d{2})",
        # 中文常见：发布于2024-06-24 / 发布时间：2024/06/24
        r"(?:发布于|发布时间|发表于|日期)[：:\s]*(\d{4})[-/](\d{1,2})[-/](\d{1,2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                year, month, day = [int(x) for x in match.groups()]
                return datetime(year, month, day).date()
            except ValueError:
                pass

    # URL 路径中的日期
    parsed_path = urlparse(article.url or "").path
    url_patterns = [
        r"/(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})",  # /2024/06/24/
        r"/(20\d{2})(\d{2})(\d{2})",                   # /20240624/
        r"/(20\d{2})[-_/](\d{1,2})(?:/|$)",             # /2024/06/
    ]
    for pattern in url_patterns:
        match = re.search(pattern, parsed_path)
        if match:
            try:
                groups = [int(x) for x in match.groups()]
                if len(groups) == 3:
                    return datetime(groups[0], groups[1], groups[2]).date()
                elif len(groups) == 2:
                    return datetime(groups[0], groups[1], 1).date()
            except ValueError:
                pass

    # "X月X日" 无年份（补当年）
    month_day = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text)
    if month_day:
        try:
            now = datetime.now().astimezone().date()
            return datetime(now.year, int(month_day.group(1)), int(month_day.group(2))).date()
        except ValueError:
            pass

    # 英文日期
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
        "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    english_date = re.search(
        r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sept|sep|october|oct|november|nov|december|dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?",
        text, re.I,
    )
    if english_date:
        try:
            now = datetime.now().astimezone().date()
            year = int(english_date.group(3) or now.year)
            month = months[english_date.group(1).lower().rstrip(".")]
            return datetime(year, month, int(english_date.group(2))).date()
        except (ValueError, KeyError):
            pass

    # 相对日期："X分钟前", "X小时前", "X天前"
    now = datetime.now().astimezone()
    relative_minutes = re.search(r"(\d+)\s*分钟前", text)
    if relative_minutes:
        dt = now - timedelta(minutes=int(relative_minutes.group(1)))
        return dt.date()
    
    relative_hours = re.search(r"(\d+)\s*小时前", text)
    if relative_hours:
        dt = now - timedelta(hours=int(relative_hours.group(1)))
        return dt.date()
    
    relative_days = re.search(r"(\d+)\s*天前", text)
    if relative_days:
        dt = now - timedelta(days=int(relative_days.group(1)))
        return dt.date()

    return None


def filter_recent_articles(articles: list[RawArticle], recency_days: int = 2) -> tuple[list[RawArticle], list[str]]:
    """只过滤明确识别为范围外日期的文章；无法识别日期的保留，交给人工判断。"""
    today = datetime.now().astimezone().date()
    recency_days = max(1, min(30, int(recency_days or 2)))
    earliest = today - timedelta(days=recency_days - 1)
    kept = []
    old_count = 0
    unknown_count = 0

    for article in articles:
        article_date = _extract_article_date(article)
        if article_date is None:
            unknown_count += 1
            kept.append(article)
            continue
        if earliest <= article_date <= today:
            kept.append(article)
        else:
            old_count += 1

    messages = []
    if old_count:
        range_label = "今天/昨天" if recency_days == 2 else f"最近 {recency_days} 天"
        messages.append(f"已过滤 {old_count} 条明确不在{range_label}范围内的旧内容。")
    if unknown_count:
        messages.append(f"{unknown_count} 条内容未识别到日期，已暂时保留供人工判断。")
    return kept, messages


def filter_recruitment_articles(articles: list[RawArticle]) -> tuple[list[RawArticle], int]:
    kept = []
    removed = 0
    for article in articles:
        text = " ".join([
            article.title or "",
            article.content_snippet or "",
            article.source_name or "",
            article.url or "",
        ])
        if RECRUITMENT_RE.search(text):
            removed += 1
            continue
        kept.append(article)
    return kept, removed


LOGISTICS_TITLE_SCREEN_SYSTEM_PROMPT = """
你是百运科技公众号“每日跨境实用情报”的信息筛选助手。
你的任务不是写文章，而是从候选标题中筛选出最值得编辑人继续查看的高价值信息源。

目标读者：跨境电商卖家、外贸企业、物流客户、公司业务/运营同事。

最高优先级：
1. 各国海关新规解读
2. 清关避坑指南
3. 跨境电商新政
4. 货物合规实操
5. 物流渠道选择技巧
6. 产品细节拆解
7. 渠道资源亮点

允许入选：
- 海关新规、清关要求、关税调整、贸易救济
- 亚马逊/FBA/沃尔玛/Temu/TikTok Shop/Ozon 等平台物流或合规规则
- 扣关、补资料、申报、VAT、EORI、认证、HS 编码、包装要求
- 特殊货物运输政策，如带电、危险品、液体、粉末、磁性产品
- 港口拥堵、航线调整、舱位紧张、航运事故、红海/海峡风险
- 油价、汇率、旺季运费、物流成本变化
- 能转化为客户避坑、降本、选渠道、合规提醒的内容

必须排除：
- 招聘、求职、岗位、职位推荐、人才招募、简历投递、加入我们等信息，一律 reject，不得进入日报
- 泛娱乐、社会新闻、无业务影响的国际新闻
- 纯企业融资、财报、人物采访，除非明确影响跨境物流/平台/清关
- 纯观点文章，没有具体新规、事件、规则、数据或操作价值
- 旧闻、重复新闻、标题党
- 与跨境电商、物流、清关、关税、平台规则无关的内容

每天最终建议总共只推荐 3-5 条。宁缺毋滥。
请严格输出 JSON，不要输出解释文字。
输出格式：
{"selected":[{"source_url":"原链接","score":0-10,"category":"分类","reason":"筛选理由"}],"rejected":[{"source_url":"原链接","reason":"淘汰原因"}]}
"""

AI_WEEKLY_TITLE_SCREEN_SYSTEM_PROMPT = """
你是百运科技《AI热点技术周报》的信息筛选助手。
你的任务不是写文章，而是从候选标题中筛选最值得编辑人继续查看的 AI 技术信息源。

目标读者：关注大模型、Agent、AI 编程工具、多模态、企业 AI 应用、机器人/具身智能的技术同事和业务同事。

优先保留（约 70-80%）：
- 前沿大模型发布、升级、API/平台上线
- Agent 产品、Agent 平台、computer-use / browser-use / desktop-use
- AI 编程工具、coding model、开发者工具、benchmark
- 多模态生成（语音/图像/视频）、企业 AI 功能
- 开源模型、model card、技术报告、GitHub repo
- 与模型能力直接相关的机器人 / VLA / world model 发布

可保留（约 10-20%）：
- AI 基础设施、推理、embedding / reranker / RAG、评测工具

少量保留（约 0-20%）：
- 机器人 / 具身智能，但必须有明确模型、系统或产品发布

必须排除：
- 纯融资、纯政策、无技术细节的行业战略稿
- 谣言、标题党、旧闻重复
- 与 AI 模型 / 产品 / 工具无关的泛娱乐、社会新闻
- 仅物流 / 跨境业务、没有 AI 技术价值的内容

AI 周报建议保留 8-12 条高价值候选供人工继续筛选。宁缺毋滥。
请严格输出 JSON，不要输出解释文字。
输出格式：
{"selected":[{"source_url":"原链接","score":0-10,"category":"分类","reason":"筛选理由"}],"rejected":[{"source_url":"原链接","reason":"淘汰原因"}]}
"""


def is_ai_weekly_module(task_type: str) -> bool:
    return task_type == "ai-weekly"


def get_title_screen_config(task_type: str) -> tuple[str, int, str]:
    """返回 (system_prompt, max_selected, mode_label)。"""
    if is_ai_weekly_module(task_type):
        return AI_WEEKLY_TITLE_SCREEN_SYSTEM_PROMPT, 10, "AI 周报"
    return LOGISTICS_TITLE_SCREEN_SYSTEM_PROMPT, 5, "跨境物流"


def screen_articles_with_llm(articles: list[RawArticle], task_type: str) -> tuple[list[RawArticle], list[str], int]:
    if not articles:
        return [], [], 0

    system_prompt, max_selected, mode_label = get_title_screen_config(task_type)
    fallback = articles[:max_selected]

    llm_config = load_llm_config()
    api_key = llm_config.get("api_key", "")
    if not api_key or api_key in {"***", "replace_me"}:
        return fallback, [f"未配置 LLM API Key，{mode_label}标题筛选已跳过，仅保留前 {max_selected} 条候选。"], 0

    from routes.llm import get_llm_client

    client = get_llm_client()
    if not client:
        return fallback, [f"LLM 客户端不可用，{mode_label}标题筛选已跳过，仅保留前 {max_selected} 条候选。"], 0

    payload = [
        {
            "title": a.title,
            "source_name": a.source_name,
            "source_url": a.url,
            "source_module": a.module or task_type,
        }
        for a in articles
    ]

    try:
        response = client.chat.completions.create(
            model=llm_config.get("model", "deepseek-chat"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=1200,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0]
        result = json.loads(content)
        selected_urls = [
            item.get("source_url")
            for item in result.get("selected", [])
            if isinstance(item, dict) and item.get("source_url")
        ]
        selected_set = set(selected_urls)
        selected_articles = [a for a in articles if a.url in selected_set]
        tokens = response.usage.total_tokens if response.usage else 0
        if not selected_articles:
            return fallback, [f"{mode_label}标题筛选未选出内容，已保留前 {max_selected} 条候选供人工判断。"], tokens
        kept = selected_articles[:max_selected]
        return kept, [f"{mode_label}标题筛选：从 {len(articles)} 条中保留 {len(kept)} 条。"], tokens
    except Exception as exc:
        return fallback, [f"{mode_label}标题筛选失败，已保留前 {max_selected} 条候选：{exc}"], 0


# ═══════════════════════════════════════════════════════════
# Stage ④ — AI 分析 + 打分（使用 batch 接口）
# ═══════════════════════════════════════════════════════════

SCORE_PROMPT_SUFFIX = """
额外要求：请给这条内容打一个 0-10 分的业务影响分数（ai_score），并说明理由（ai_reason）。

打分标准（严格）：
  8-10: 直接影响客户成本/利润/发货/清关。例如：
        - 关税税率变化、新征关税
        - 海关清关新规、货物可能被扣
        - 平台强制规则变更(FBA/FBM)、影响卖家发货
        - 运费/运价大幅波动、港口停运/罢工
        - 新合规要求(HS编码/认证/申报)
  5-7:  间接影响或行业趋势。例如：平台新功能、行业数据报告、某大卖动态
  0-4:  与跨境物流/卖家利润无明显关系。例如：纯企业融资财报、纯技术产品发布、泛社会新闻
  0:   以下内容直接打 0 分，严禁进入日报：
        - 招聘/求职/职位推荐（包括"热门职位""诚聘""急招""招聘"等）
        - 公司宣传/软文推广/广告（包含"限时优惠""免费试用""扫码领取"等）
        - 与跨境物流完全无关的泛行业资讯

重要：大部分内容应该是 0-4 分。只有真正影响客户赚钱/发货/清关的内容才能到 8+。
请仍然保持原有输出格式，额外添加 ai_score 和 ai_reason 字段。
"""


def analyze_with_llm(articles: list[RawArticle], task_type: str) -> tuple[list[CandidateItem], list[str], int]:
    """逐条送 LLM，生成结构化候选 + AI 打分。返回 (candidates, errors, tokens_used)

    注意：本函数同步执行 LLM 调用（阻塞），请仅在 async 路由中通过
    run_in_threadpool(analyze_with_llm, ...) 调用，避免阻塞事件循环。
    """
    from routes.llm import process_article_impl, ProcessRequest

    llm_config = load_llm_config()
    api_key = llm_config.get("api_key", "")
    if not api_key or api_key in {"***", "replace_me"}:
        return (
            [_fallback_candidate_from_raw_article(a, "未配置 LLM API Key，当前条目未做自动摘要和影响评分。") for a in articles],
            ["未配置 LLM API Key，已跳过 AI 摘要并保留原始信息源。可设置 LLM_API_KEY 后恢复自动摘要。"],
            0,
        )

    config = load_config("sources.config.json")
    llm_task_map = {}
    for module, group in config.get("sources", {}).items():
        if isinstance(group, list):
            for src in group:
                if isinstance(src, dict):
                    llm_task_map[src.get("id", "")] = src.get("llm_task", module)

    candidates = []
    errors = []
    tokens_used = 0

    for a in articles:
        try:
            llm_task = llm_task_map.get(a.source_id, task_type)
            req = ProcessRequest(
                raw_text=f"标题：{a.title}\n\n正文：{a.content_snippet or a.title}",
                task_type=llm_task,
                source_url=a.url,
            )
            req.raw_text += f"\n\n{SCORE_PROMPT_SUFFIX}"

            result = process_article_impl(req)
            tokens_used += int(result.tokens_used or 0)

            if result.success and result.result:
                r = result.result
                candidate = CandidateItem(
                    title=r.get("title", a.title),
                    source_url=a.url,
                    source_id=a.source_id,
                    source_name=a.source_name,
                    ai_summary=r.get("summary", r.get("source_summary", "")),
                    ai_analysis=r.get("analysis", r.get("impact_on_logistics", "")),
                    ai_tags=_extract_tags(r),
                    ai_score=float(r.get("ai_score", 5.0)),
                    ai_reason=r.get("ai_reason", ""),
                    body_text=a.content_snippet or "",
                )
                candidates.append(candidate)
            else:
                raise RuntimeError(result.error or "LLM 返回 success=false")
        except Exception as e:
            errors.append(f"⚠️ {a.source_name} - {a.title[:30]}: LLM 失败: {e}")
            candidates.append(_fallback_candidate_from_raw_article(a, "LLM 调用失败，已保留原始信息源，请人工判断是否加入素材篮。"))

    return candidates, errors, tokens_used


def _fallback_candidate_from_raw_article(article: RawArticle, status: str) -> CandidateItem:
    """LLM 不可用时仍保留原始信息源，提取正文核心段落作为摘要。"""
    snippet = (article.content_snippet or "").strip()
    # 清理正文：去掉开头可能混入的导航/菜单文字
    if snippet:
        lines = snippet.split("\n")
        # 跳过开头很短的行（通常是导航碎片）
        cleaned = []
        started = False
        for line in lines:
            line = line.strip()
            if not started and len(line) < 20:
                continue
            started = True
            cleaned.append(line)
        snippet = " ".join(cleaned).strip()
    # 取前 1000 字符作为摘要
    if len(snippet) > 1000:
        snippet = snippet[:1000].rstrip() + "…"
    if not snippet or len(snippet) < 30:
        snippet = ""
    return CandidateItem(
        title=article.title or "未命名信息源",
        source_url=article.url,
        source_id=article.source_id,
        source_name=article.source_name,
        image=article.image or "",
        ai_summary=snippet,
        ai_analysis="",
        ai_tags=[],
        ai_score=5.0,
        ai_reason=status,
        body_text=article.content_snippet or "",
    )


def _extract_tags(result: dict) -> list[str]:
    tags = result.get("tags", [])
    if isinstance(tags, list):
        return [str(t) for t in tags if t]
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return []


# ═══════════════════════════════════════════════════════════
# Stage ⑤ — 打分过滤 + 排序
# ═══════════════════════════════════════════════════════════

def filter_and_sort(candidates: list[CandidateItem], threshold: float) -> list[CandidateItem]:
    for c in candidates:
        if not c.title:
            c.title = c.ai_summary or "未命名候选条目"
        if not c.ai_summary:
            c.ai_summary = c.ai_analysis or c.title
        if c.ai_tags is None:
            c.ai_tags = []

    filtered = [c for c in candidates if c.ai_score >= threshold]
    filtered.sort(key=lambda x: x.ai_score, reverse=True)
    return filtered


# ═══════════════════════════════════════════════════════════
# Stage ⑤.5 — 报告顶部概览：常见汇率
# ═══════════════════════════════════════════════════════════


async def _fetch_exchange_rates(client: httpx.AsyncClient) -> list[dict]:
    end = datetime.now().astimezone().date()
    start = end - timedelta(days=10)
    resp = await client.get(
        f"https://api.frankfurter.app/{start.isoformat()}..{end.isoformat()}",
        params={"from": "USD", "to": "CNY,EUR,GBP,JPY,HKD"},
    )
    resp.raise_for_status()
    data = resp.json()
    history = data.get("rates") or {}
    dates = sorted(history.keys())
    if not dates:
        return []

    latest_date = dates[-1]
    previous_date = dates[-2] if len(dates) >= 2 else ""
    latest_rates = history.get(latest_date) or {}
    previous_rates = history.get(previous_date) or {}

    rows = [
        {"pair": "USD/CNY", "base": "USD", "quote": "CNY", "rate": latest_rates.get("CNY"), "previous_rate": previous_rates.get("CNY")},
        {"pair": "USD/EUR", "base": "USD", "quote": "EUR", "rate": latest_rates.get("EUR"), "previous_rate": previous_rates.get("EUR")},
        {"pair": "USD/GBP", "base": "USD", "quote": "GBP", "rate": latest_rates.get("GBP"), "previous_rate": previous_rates.get("GBP")},
        {"pair": "USD/JPY", "base": "USD", "quote": "JPY", "rate": latest_rates.get("JPY"), "previous_rate": previous_rates.get("JPY")},
        {"pair": "USD/HKD", "base": "USD", "quote": "HKD", "rate": latest_rates.get("HKD"), "previous_rate": previous_rates.get("HKD")},
    ]
    enriched = []
    for row in rows:
        rate = row.get("rate")
        previous_rate = row.get("previous_rate")
        if rate is None:
            continue
        change = None
        change_percent = None
        if previous_rate not in (None, 0):
            change = rate - previous_rate
            change_percent = change / previous_rate * 100
        enriched.append({
            **row,
            "date": latest_date,
            "previous_date": previous_date,
            "change": change,
            "change_percent": change_percent,
            "source": "Frankfurter",
        })
    return enriched


async def fetch_report_briefing() -> ReportBriefing:
    briefing = ReportBriefing(report_date=datetime.now().astimezone().strftime("%Y-%m-%d"))
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            briefing.exchange_rates = await _fetch_exchange_rates(client)
        except Exception as exc:
            briefing.errors.append(f"汇率获取失败：{exc}")

    return briefing


# ═══════════════════════════════════════════════════════════
# Stage ⑥ — 持久化：保存候选结果到 data/drafts/
# ═══════════════════════════════════════════════════════════

def save_draft(fetch_result: FetchResult) -> tuple[str, Optional[str]]:
    """将管道结果保存为 JSON 草稿。返回 (filename, error_message)。"""
    try:
        DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{fetch_result.task_type}_{ts}.json"
        path = DRAFTS_DIR / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fetch_result.model_dump(mode="json"), f, ensure_ascii=False, indent=2)

        if fetch_result.candidates:
            latest_path = DRAFTS_DIR / f"{fetch_result.task_type}_latest.json"
            with open(latest_path, "w", encoding="utf-8") as f:
                json.dump(fetch_result.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
        return filename, None
    except OSError as exc:
        return "", f"草稿保存失败（抓取结果仍可用）：{exc}"


def list_drafts(task_type: Optional[str] = None) -> list[dict]:
    """列出已有的草稿"""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    drafts = []
    for f in sorted(DRAFTS_DIR.glob("*.json"), reverse=True):
        if task_type and not f.name.startswith(task_type):
            continue
        if f.name.endswith("_latest.json"):
            continue
        stat = f.stat()
        candidate_count = 0
        error_count = 0
        try:
            with open(f, "r", encoding="utf-8") as payload_file:
                payload = json.load(payload_file)
            candidate_count = len(payload.get("candidates") or [])
            error_count = len(payload.get("errors") or [])
        except Exception:
            pass
        drafts.append({
            "filename": f.name,
            "task_type": f.name.split("_")[0] if "_" in f.name else "",
            "size": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "candidate_count": candidate_count,
            "error_count": error_count,
        })
    return drafts


def load_latest_non_empty_draft(task_type: str) -> tuple[list[CandidateItem], str]:
    """读取最近一次有候选的草稿，用于缓存命中导致本次为空时回退展示。"""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        DRAFTS_DIR.glob(f"{task_type}_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in files:
        if path.name.endswith("_latest.json"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            raw_candidates = payload.get("candidates") or []
            if raw_candidates:
                return [CandidateItem(**item) for item in raw_candidates], path.name
        except Exception:
            continue
    return [], ""


# ═══════════════════════════════════════════════════════════
# 主接口 — 管道入口
# ═══════════════════════════════════════════════════════════

@router.post("/fetch", response_model=FetchResult)
async def fetch_pipeline(req: FetchRequest):
    """
    一键获取内容管道:
        ① 读 sources.config.json → 选出匹配来源
        ② 并发抓取网页 → 专用选择器提取标题+链接
        ③ URL 去重
        ④ 缓存过滤（跳过已处理 URL）
        ⑤ 并发抓取正文
        ⑥ 逐条 LLM 分析 + 打分
        ⑦ 按阈值过滤排序
        ⑧ 持久化到 data/drafts/
        ⑨ 返回候选池
    """
    # ① 选来源
    sources = pick_sources(req.task_type, req.source_ids)
    if not sources:
        raise HTTPException(404, f"未找到可用来源: task_type={req.task_type}")

    briefing = await fetch_report_briefing()

    # ② 并发抓取
    raw_articles, fetch_errors = await fetch_raw_articles(sources, req.limit)
    total_raw = len(raw_articles)

    # ③ 去重
    unique_articles = deduplicate_by_url(raw_articles)
    after_dedup = len(unique_articles)

    unique_articles, recruitment_count = filter_recruitment_articles(unique_articles)
    if recruitment_count:
        fetch_errors.append(f"已过滤 {recruitment_count} 条招聘/求职/岗位类内容。")

    if req.excluded_urls:
        excluded = set(req.excluded_urls)
        excluded_normalized = {normalize_url(url) for url in req.excluded_urls}
        before_excluded = len(unique_articles)
        unique_articles = [
            article for article in unique_articles
            if article.url not in excluded and normalize_url(article.url) not in excluded_normalized
        ]
        skipped = before_excluded - len(unique_articles)
        if skipped:
            fetch_errors.append(f"已跳过 {skipped} 条本轮不感兴趣的候选。")

    pre_screen_articles = list(unique_articles)
    screened_urls = set()

    # ④ 缓存过滤（非 force_refresh 时生效）
    cached_count = 0
    if not req.force_refresh:
        unique_articles, cached_count = filter_cached(unique_articles)
        if cached_count > 0:
            fetch_errors.append(f"⏭ 跳过 {cached_count} 篇已缓存文章（使用 force_refresh=true 强制刷新）")

    # ⑤ 抓正文前先用标题列表做轻量筛选，减少无用正文抓取和后续 token
    tokens_used = 0
    if req.screen_with_llm and unique_articles:
        unique_articles, screen_messages, screen_tokens = await run_in_threadpool(
            screen_articles_with_llm,
            unique_articles,
            req.task_type,
        )
        fetch_errors.extend(screen_messages)
        tokens_used += screen_tokens
        screened_urls = {article.url for article in unique_articles}

    # ⑥ 并发抓取正文
    unique_articles = await enrich_article_content(unique_articles)
    unique_articles, body_recruitment_count = filter_recruitment_articles(unique_articles)
    if body_recruitment_count:
        fetch_errors.append(f"正文阶段已过滤 {body_recruitment_count} 条招聘/求职/岗位类内容。")

    # ⑥.5 日期过滤：明确不是今天/昨天的内容不进入素材池
    unique_articles, recency_messages = filter_recent_articles(unique_articles, req.recency_days)
    fetch_errors.extend(recency_messages)

    # ⑥.6 可选正文清洗：仅对物流/政策中仍带导航/客服/公众号噪音的正文调用 LLM
    if unique_articles:
        unique_articles, cleaned_count = await run_in_threadpool(
            clean_article_contents_with_llm,
            unique_articles,
            req.task_type,
        )
        if cleaned_count:
            fetch_errors.append(f"ContentAgent 已清洗 {cleaned_count} 条正文模板噪音。")

    if not unique_articles and req.screen_with_llm and pre_screen_articles:
        fallback_articles = [
            article for article in pre_screen_articles
            if article.url not in screened_urls
        ][:5]
        if fallback_articles:
            fallback_articles = await enrich_article_content(fallback_articles)
            fallback_articles, fallback_messages = filter_recent_articles(fallback_articles, req.recency_days)
            fetch_errors.extend(fallback_messages)
            if fallback_articles:
                fallback_articles, cleaned_count = await run_in_threadpool(
                    clean_article_contents_with_llm,
                    fallback_articles,
                    req.task_type,
                )
                if cleaned_count:
                    fetch_errors.append(f"ContentAgent 已清洗 {cleaned_count} 条补充正文模板噪音。")
                unique_articles = fallback_articles
                range_label = "今天/昨天" if req.recency_days == 2 else f"最近 {req.recency_days} 天"
                fetch_errors.append(f"首轮筛选内容不符合{range_label}范围，已从剩余标题中补充候选。")

    # ⑦ AI 分析 + 打分（用转换后的标准 task_type 匹配 prompt）
    # analyze_with_llm 内部是同步阻塞 LLM 调用，放线程池执行避免卡死事件循环
    resolved_task_type = resolve_task_type(req.task_type)
    if req.analyze_with_llm:
        candidates, llm_errors, analyze_tokens = await run_in_threadpool(
            analyze_with_llm, unique_articles, resolved_task_type
        )
        tokens_used += analyze_tokens
    else:
        candidates = [
            _fallback_candidate_from_raw_article(a, "仅获取信息源，未调用 AI 摘要和影响评分。")
            for a in unique_articles
        ]
        llm_errors = ["已按“只获取信息源”模式运行，本次未消耗 LLM token。"] if unique_articles else []
    all_errors = fetch_errors + llm_errors

    # ⑦ 过滤排序
    candidates = filter_and_sort(candidates, req.score_threshold)
    from_cache = False
    draft_filename = ""

    if not candidates and cached_count > 0 and not req.force_refresh:
        cached_candidates, draft_filename = load_latest_non_empty_draft(req.task_type)
        if cached_candidates:
            candidates = cached_candidates
            from_cache = True
            all_errors.append(
                f"本次抓到的 {cached_count} 篇均已缓存，已自动显示最近候选池：{draft_filename}。如需重新分析，请勾选“忽略缓存，重新抓取”。"
            )

    # ⑧ 持久化
    result = FetchResult(
        task_type=req.task_type,
        sources_used=len(sources),
        total_raw=total_raw,
        after_dedup=after_dedup,
        candidates=candidates,
        errors=all_errors,
        from_cache=from_cache,
        cached_count=cached_count,
        draft_filename=draft_filename,
        briefing=briefing,
        llm_enabled=req.analyze_with_llm,
        tokens_used=tokens_used,
    )
    if not from_cache:
        saved_name, save_error = save_draft(result)
        if saved_name:
            draft_filename = saved_name
        if save_error:
            all_errors.append(save_error)
            result = result.model_copy(update={"errors": all_errors, "draft_filename": draft_filename})

    # ⑨ 标记已处理的 URL（缓存写入）
    if not req.force_refresh and not from_cache:
        urls = [c.source_url for c in candidates]
        try:
            _save_to_cache(urls)
        except OSError as exc:
            all_errors.append(f"URL 缓存写入失败：{exc}")
            result = result.model_copy(update={"errors": all_errors})

    return result


# ═══════════════════════════════════════════════════════════
# 辅助接口 — 草稿列表 & 来源测试
# ═══════════════════════════════════════════════════════════

@router.get("/drafts")
def get_drafts(task_type: Optional[str] = None):
    """列出已保存的草稿"""
    return list_drafts(task_type)


@router.get("/drafts/{filename}")
def get_draft(filename: str):
    """加载指定草稿的完整内容"""
    # 安全校验：只允许 letters/digits/_-/. 的文件名，禁止 .. 路径遍历
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", filename) or ".." in filename:
        raise HTTPException(400, "非法草稿文件名")
    path = (DRAFTS_DIR / filename).resolve()
    # 二次校验：解析后的真实路径必须仍在 DRAFTS_DIR 内
    try:
        path.relative_to(DRAFTS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "非法草稿文件名")
    if not path.exists():
        raise HTTPException(404, f"草稿不存在: {filename}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def _try_playwright_fallback(src: dict, source_id: str, max_items: int = 10) -> TestSourceResponse:
    """尝试用 Playwright 抓取来源，返回统一的 TestSourceResponse。"""
    articles, errors = await _fetch_one_source_playwright(src, max_items)
    return TestSourceResponse(
        source_id=source_id,
        reachable=bool(articles),
        article_count=len(articles),
        sample_titles=[a.title[:80] for a in articles[:5]],
        error="；".join(errors) if errors and not articles else None,
    )


@router.post("/test-source", response_model=TestSourceResponse)
async def test_source(req: TestSourceRequest):
    """来源健康检查：抓取一个来源并返回提取到的文章数+标题样例"""
    src = {
        "id": req.source_id,
        "name": req.source_id,
        "url": req.source_url,
    }
    use_playwright = _host_matches(req.source_url, PLAYWRIGHT_SOURCE_DOMAINS)

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                req.source_url,
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            if use_playwright:
                return await _try_playwright_fallback(src, req.source_id)
            return TestSourceResponse(source_id=req.source_id, reachable=False, error="请求超时")
        except httpx.HTTPStatusError as e:
            if use_playwright:
                return await _try_playwright_fallback(src, req.source_id)
            return TestSourceResponse(source_id=req.source_id, reachable=False, error=f"HTTP {e.response.status_code}")
        except Exception as e:
            if use_playwright:
                return await _try_playwright_fallback(src, req.source_id)
            return TestSourceResponse(source_id=req.source_id, reachable=False, error=str(e))

    articles = _extract_articles_from_html(src, _decode_response_text(resp), str(resp.url), 10)
    if not articles and use_playwright:
        pw_resp = await _try_playwright_fallback(src, req.source_id)
        if pw_resp.article_count > 0:
            return pw_resp
        return TestSourceResponse(
            source_id=req.source_id,
            reachable=False,
            article_count=0,
            sample_titles=[],
            error=pw_resp.error or "Playwright 渲染后仍未提取到文章",
        )

    return TestSourceResponse(
        source_id=req.source_id,
        reachable=True,
        article_count=len(articles),
        sample_titles=[a.title[:80] for a in articles[:5]],
    )


@router.post("/clear-cache")
def clear_cache():
    """清除 URL 处理缓存"""
    cache_file = CACHE_DIR / "processed_urls.txt"
    if cache_file.exists():
        cache_file.unlink()
        return {"success": True, "message": "缓存已清除"}
    return {"success": True, "message": "缓存不存在（无需清除）"}
