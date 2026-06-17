"""
内容采集管道 — Phase 2: 并行化 + 专用选择器 + 持久化 + 缓存

POST /api/scrape/fetch 
    管道: 读配置 → 并发抓取 → 去重 → 缓存过滤 → AI评分 → 过滤 → 持久化 → 返回候选
"""

import json
import re
import hashlib
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from models import RawArticle, CandidateItem, FetchResult

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DRAFTS_DIR = DATA_DIR / "drafts"
CACHE_DIR = DATA_DIR / "cache"

router = APIRouter(prefix="/api/scrape", tags=["采集管道"])

PLAYWRIGHT_SOURCE_DOMAINS = {
    "openai.com",
    "venturebeat.com",
    "jiqizhixin.com",
    "qbitai.com",
    "huxiu.com",
    "kjdsnews.com",
    "microsoft.ai",
    "blogs.microsoft.com",
    "research.google",
    "developer.nvidia.com",
    "x.ai",
}
PLAYWRIGHT_ARTICLE_DOMAINS = set(PLAYWRIGHT_SOURCE_DOMAINS)
PLAYWRIGHT_CONCURRENCY = asyncio.Semaphore(2)

# ── task_type 别名映射 ────────────────────────────────────
# sources.config.json 的模块 key 可能与 LLM 的 VALID_TASK_TYPES 不同，
# 此映射确保前端传 logistics-daily / weather-alert 时能正确匹配到 prompt。
TASK_TYPE_ALIASES: dict[str, str] = {
    "logistics-daily": "cn-logistics-industry",
    "weather-alert": "global-logistics-risk",
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

# 来源域名 → 自定义提取函数
# 每个函数签名: (soup: BeautifulSoup, base_url: str, max_items: int) -> list[RawArticle]

def _extract_openai(soup, base_url, max_items):
    """OpenAI Blog: 列表页是 <article> 或 <li> 含 <a> + <time>"""
    articles = []
    for item in soup.select("article a[href], li a[href]"):
        title = " ".join(item.get_text(" ", strip=True).split())
        href = urljoin(base_url, item["href"].strip())
        if not title or len(title) < 15:
            continue
        if not href.startswith("https://openai.com/index/"):
            continue
        key = f"{title[:60]}{href}"
        if any(a.url == href for a in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_venturebeat(soup, base_url, max_items):
    """VentureBeat: 文章链接在 h2/h3 > a 中"""
    articles = []
    for a in soup.select("h2 a, h3 a, .article-title a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 15:
            continue
        if not href.startswith("https://venturebeat.com/"):
            continue
        if any(a.url == href for a in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_jiqizhixin(soup, base_url, max_items):
    """机器之心: 文章在 .article-title 或 h2/h3 > a"""
    articles = []
    for a in soup.select(".article-title a, h2 a, h3 a, .title a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 10:
            continue
        if not href.startswith("https://www.jiqizhixin.com/"):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_qbitai(soup, base_url, max_items):
    """量子位: 文章在 .article-list 或 h2/h3 > a"""
    articles = []
    for a in soup.select(".article-list a, h2 a, h3 a, .title a, .news-title a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 10:
            continue
        if not href.startswith("https://www.qbitai.com/"):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_huxiu(soup, base_url, max_items):
    """虎嗅: 文章在 .article-item 或 h2/h3 > a"""
    articles = []
    for a in soup.select(".article-item a, h2 a, h3 a, .title a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 10:
            continue
        if not href.startswith("https://www.huxiu.com/"):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_microsoft_ai(soup, base_url, max_items):
    """Microsoft AI Blog: 文章在 article 或 blog-post 中"""
    articles = []
    for a in soup.select("article a[href], .blog-post a[href], h2 a, h3 a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 15:
            continue
        if not (href.startswith("https://microsoft.ai/") or href.startswith("https://blogs.microsoft.com/")):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_google_research(soup, base_url, max_items):
    """Google Research Blog: 文章在 h2 > a 或 .post-title > a"""
    articles = []
    for a in soup.select("h2 a, h3 a, .post-title a, .entry-title a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 15:
            continue
        if not href.startswith("https://research.google/blog/"):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_nvidia_dev(soup, base_url, max_items):
    """NVIDIA Developer Blog: 文章在 h2/h3 > a"""
    articles = []
    for a in soup.select("h2 a, h3 a, .post-title a, .entry-title a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 15:
            continue
        if not href.startswith("https://developer.nvidia.com/blog/"):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_anthropic(soup, base_url, max_items):
    """Anthropic Research: 文章在 h2 > a 或 article > a"""
    articles = []
    for a in soup.select("h2 a, h3 a, article a[href], .post-card a"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 15:
            continue
        if not href.startswith("https://www.anthropic.com/research/"):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


def _extract_xai(soup, base_url, max_items):
    """xAI Blog: 文章在 h2/h3 > a 或 article"""
    articles = []
    for a in soup.select("h2 a, h3 a, article a[href]"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())
        if not title or len(title) < 10:
            continue
        if not href.startswith("https://x.ai/"):
            continue
        if any(a_art.url == href for a_art in articles):
            continue
        articles.append(RawArticle(title=title, url=href))
        if len(articles) >= max_items:
            break
    return articles


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


def _extract_generic(soup, base_url, max_items):
    """通用提取：所有 <a> 标签，过滤噪音"""
    articles = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"].strip())

        # 过滤噪音
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
_EXTRACTORS = {
    "openai.com": _extract_openai,
    "venturebeat.com": _extract_venturebeat,
    "jiqizhixin.com": _extract_jiqizhixin,
    "qbitai.com": _extract_qbitai,
    "huxiu.com": _extract_huxiu,
    "kjdsnews.com": _extract_kjdsnews,
    "microsoft.ai": _extract_microsoft_ai,
    "blogs.microsoft.com": _extract_microsoft_ai,
    "research.google": _extract_google_research,
    "developer.nvidia.com": _extract_nvidia_dev,
    "anthropic.com": _extract_anthropic,
    "x.ai": _extract_xai,
}


def _get_extractor(url: str):
    """根据 URL 域名匹配专用提取器，无匹配则返回通用提取器"""
    host = urlparse(url).hostname or ""
    for domain, extractor in _EXTRACTORS.items():
        if domain in host:
            return extractor
    return _extract_generic


def _host_matches(url: str, domains: set[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(domain in host for domain in domains)


# ── Playwright 浏览器池（单例复用，避免每次请求启动新浏览器） ─────

import atexit

class _BrowserPool:
    """懒初始化单例浏览器池。多个请求共享一个浏览器实例，各自使用独立 context/page。"""

    def __init__(self):
        self._playwright = None
        self._browser = None

    def _ensure_started(self):
        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError("未安装 playwright，请先安装依赖") from exc
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)

    def render_page(self, url: str, wait_ms: int = 1200) -> tuple[str, str]:
        """在共享浏览器中打开页面，返回 (html, final_url)。"""
        self._ensure_started()
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        context = self._browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36"
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(wait_ms)
            return page.content(), page.url
        finally:
            context.close()

    def shutdown(self):
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None


_browser_pool = _BrowserPool()
atexit.register(_browser_pool.shutdown)


def _render_with_playwright(url: str, wait_ms: int = 1200) -> tuple[str, str]:
    """使用 Playwright 渲染页面，返回 (html, final_url)。复用浏览器实例。"""
    return _browser_pool.render_page(url, wait_ms)


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
            html, final_url = await run_in_threadpool(_render_with_playwright, src_url)
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
        articles = _extract_articles_from_html(src, resp.text, str(resp.url), limit)

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

def extract_body_text(html: str) -> str:
    """从文章页 HTML 提取正文文本"""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    body = soup.find("article") or soup.find(class_="article-content") or \
           soup.find(class_="content") or soup.find(class_="post-content") or soup

    text = body.get_text(" ", strip=True)
    return text[:3000] if len(text) > 3000 else text


async def _enrich_one(article: RawArticle, client: httpx.AsyncClient) -> RawArticle:
    """并发抓取单篇文章正文"""
    try:
        resp = await client.get(
            article.url,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        )
        resp.raise_for_status()
        article.content_snippet = extract_body_text(resp.text)
    except Exception:
        if _host_matches(article.url, PLAYWRIGHT_ARTICLE_DOMAINS):
            try:
                html, _ = await run_in_threadpool(_render_with_playwright, article.url, 800)
                article.content_snippet = extract_body_text(html)
            except Exception:
                pass  # Playwright 失败也不丢条目
    return article


async def enrich_article_content(articles: list[RawArticle]) -> list[RawArticle]:
    """并发抓取所有文章正文"""
    if not articles:
        return articles
    async with httpx.AsyncClient() as client:
        tasks = [_enrich_one(a, client) for a in articles]
        return list(await asyncio.gather(*tasks))


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
    """将 URL 追加到缓存（带时间戳），同时清理过期条目。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "processed_urls.txt"
    new_keys = {_cache_key(u) for u in urls}
    existing = _load_cache()
    to_add = new_keys - existing
    if not to_add:
        return

    now_ts = time.time()
    # 追加新条目（带时间戳）
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

    # 重写文件，去掉过期条目
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

    with open(cache_file, "w") as f:
        for line in kept_lines:
            f.write(line + "\n")


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
            all_sources = ", ".join(set(a.source_name for a in group))
            primary.source_name = f"{primary.source_name} (同步自 {all_sources})"
        merged.append(primary)

    return merged


# ═══════════════════════════════════════════════════════════
# Stage ④ — AI 分析 + 打分（使用 batch 接口）
# ═══════════════════════════════════════════════════════════

SCORE_PROMPT_SUFFIX = """
额外要求：请给这条内容打一个 0-10 分的业务影响分数（ai_score），并说明理由（ai_reason）。
打分标准：
  8-10: 直接影响报价/履约/成本/清关，必须关注
  5-7:  行业趋势或间接影响，值得了解
  0-4:  无关或仅作背景信息
注意：请仍然保持原有输出格式，额外添加 ai_score 和 ai_reason 字段。
"""


def analyze_with_llm(articles: list[RawArticle], task_type: str) -> tuple[list[CandidateItem], list[str]]:
    """逐条送 LLM，生成结构化候选 + AI 打分。返回 (candidates, errors)"""
    from routes.llm import process_article, ProcessRequest

    config = load_config("sources.config.json")
    llm_task_map = {}
    for module, group in config.get("sources", {}).items():
        if isinstance(group, list):
            for src in group:
                if isinstance(src, dict):
                    llm_task_map[src.get("id", "")] = src.get("llm_task", module)

    candidates = []
    errors = []

    for a in articles:
        try:
            llm_task = llm_task_map.get(a.source_id, task_type)
            req = ProcessRequest(
                raw_text=f"标题：{a.title}\n\n正文：{a.content_snippet or a.title}",
                task_type=llm_task,
                source_url=a.url,
            )
            req.raw_text += f"\n\n{SCORE_PROMPT_SUFFIX}"

            result = process_article(req)

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
                )
                candidates.append(candidate)
            else:
                raise RuntimeError(result.error or "LLM 返回 success=false")
        except Exception as e:
            errors.append(f"⚠️ {a.source_name} - {a.title[:30]}: LLM 失败: {e}")
            # 不再将失败条目加入候选池，避免数据污染。
            # 失败信息已记录在 errors 中，用户可在错误提示中查看。

    return candidates, errors


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
# Stage ⑥ — 持久化：保存候选结果到 data/drafts/
# ═══════════════════════════════════════════════════════════

def save_draft(fetch_result: FetchResult):
    """将管道结果保存为 JSON 草稿"""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{fetch_result.task_type}_{ts}.json"
    path = DRAFTS_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fetch_result.model_dump(mode="json"), f, ensure_ascii=False, indent=2)

    # 只有有候选的结果才覆盖 latest，避免一次抓取失败把可用候选池顶掉。
    if fetch_result.candidates:
        latest_path = DRAFTS_DIR / f"{fetch_result.task_type}_latest.json"
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(fetch_result.model_dump(mode="json"), f, ensure_ascii=False, indent=2)


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

    # ② 并发抓取
    raw_articles, fetch_errors = await fetch_raw_articles(sources, req.limit)
    total_raw = len(raw_articles)

    # ③ 去重
    unique_articles = deduplicate_by_url(raw_articles)
    after_dedup = len(unique_articles)

    # ④ 缓存过滤（非 force_refresh 时生效）
    cached_count = 0
    if not req.force_refresh:
        unique_articles, cached_count = filter_cached(unique_articles)
        if cached_count > 0:
            fetch_errors.append(f"⏭ 跳过 {cached_count} 篇已缓存文章（使用 force_refresh=true 强制刷新）")

    # ⑤ 并发抓取正文
    unique_articles = await enrich_article_content(unique_articles)

    # ⑥ AI 分析 + 打分（用转换后的标准 task_type 匹配 prompt）
    resolved_task_type = resolve_task_type(req.task_type)
    candidates, llm_errors = analyze_with_llm(unique_articles, resolved_task_type)
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
    )
    if not from_cache:
        save_draft(result)

    # ⑨ 标记已处理的 URL（缓存写入）
    if not req.force_refresh and not from_cache:
        urls = [c.source_url for c in candidates]
        _save_to_cache(urls)

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
    path = DRAFTS_DIR / filename
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

    articles = _extract_articles_from_html(src, resp.text, str(resp.url), 10)
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
