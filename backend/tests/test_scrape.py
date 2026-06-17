"""
scrape.py 功能测试
"""
import hashlib
import time

import pytest

from routes.scrape import (
    _cache_key,
    _extract_kjdsnews,
    TASK_TYPE_ALIASES,
    resolve_task_type,
    normalize_url,
    deduplicate_by_url,
    _load_cache,
    _save_to_cache,
    CACHE_TTL_SECONDS,
)
from models import RawArticle


# ── task_type 别名测试 ──────────────────────────────────────

class TestTaskTypeAliases:
    def test_logistics_daily_alias(self):
        assert resolve_task_type("logistics-daily") == "cn-logistics-industry"

    def test_weather_alert_alias(self):
        assert resolve_task_type("weather-alert") == "global-logistics-risk"

    def test_standard_types_pass_through(self):
        for t in ("ai-weekly", "global-logistics-risk", "cn-logistics-industry", "exchange-rate"):
            assert resolve_task_type(t) == t

    def test_unknown_type_pass_through(self):
        assert resolve_task_type("unknown-module") == "unknown-module"

    def test_aliases_cover_source_modules(self, sources_config):
        """确保 sources.config.json 中每个模块 key 都有对应的 LLM prompt"""
        source_keys = set(sources_config.get("sources", {}).keys())
        for key in source_keys:
            resolved = resolve_task_type(key)
            # 解析后应该是一个标准的 LLM task_type（包含在别名映射或自身就是标准的）
            assert resolved != key or key in ("ai-weekly", "global-logistics-risk", "cn-logistics-industry", "exchange-rate")


# ── kjdsnews 提取器测试 ────────────────────────────────────

class TestKjdsnewsExtractor:
    def _make_soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def test_extract_kjdsnews_basic(self):
        html = """
        <html><body>
        <div class="title"><a href="/a/2819843.html">紧急！亚马逊大批店铺触发视频验证</a></div>
        <div class="title"><a href="/a/2819842.html">多家平台重点布局，中大件为何成为跨境新风口？</a></div>
        <div class="title"><a href="/a/2819841.html">跨境圈最没用的事：无谓社交</a></div>
        <div class="title"><a href="/about.html">关于我们</a></div>
        </body></html>
        """
        soup = self._make_soup(html)
        articles = _extract_kjdsnews(soup, "https://www.kjdsnews.com", 5)
        assert len(articles) == 3
        assert articles[0].title == "紧急！亚马逊大批店铺触发视频验证"
        assert articles[0].url.startswith("https://www.kjdsnews.com/a/")

    def test_extract_kjdsnews_filters_short_titles(self):
        html = """
        <html><body>
        <div class="title"><a href="/a/1234.html">短标题</a></div>
        <div class="title"><a href="/a/5678.html">这是一个足够长的标题用于测试</a></div>
        </body></html>
        """
        soup = self._make_soup(html)
        articles = _extract_kjdsnews(soup, "https://www.kjdsnews.com", 5)
        assert len(articles) == 1
        assert articles[0].title == "这是一个足够长的标题用于测试"

    def test_extract_kjdsnews_filters_non_article_links(self):
        html = """
        <html><body>
        <div class="title"><a href="/news/category/跨境/">跨境资讯</a></div>
        <div class="title"><a href="/a/1234.html">正确的文章标题内容</a></div>
        </body></html>
        """
        soup = self._make_soup(html)
        articles = _extract_kjdsnews(soup, "https://www.kjdsnews.com", 5)
        assert len(articles) == 1

    def test_extract_kjdsnews_dedup(self):
        html = """
        <html><body>
        <div class="title"><a href="/a/1234.html">相同链接的标题</a></div>
        <div class="title"><a href="/a/1234.html">相同链接的另一个标题</a></div>
        </body></html>
        """
        soup = self._make_soup(html)
        articles = _extract_kjdsnews(soup, "https://www.kjdsnews.com", 5)
        assert len(articles) == 1

    def test_extract_kjdsnews_limit(self):
        html = """
        <html><body>
        <div class="title"><a href="/a/1.html">文章标题一内容测试</a></div>
        <div class="title"><a href="/a/2.html">文章标题二内容测试</a></div>
        <div class="title"><a href="/a/3.html">文章标题三内容测试</a></div>
        </body></html>
        """
        soup = self._make_soup(html)
        articles = _extract_kjdsnews(soup, "https://www.kjdsnews.com", 2)
        assert len(articles) == 2


# ── URL 处理测试 ────────────────────────────────────────────

class TestUrlProcessing:
    def test_normalize_url(self):
        assert normalize_url("https://www.example.com/path/") == "example.com/path"
        assert normalize_url("https://example.com/path") == "example.com/path"
        assert normalize_url("https://www.example.com") == "example.com"

    def test_deduplicate_by_url(self):
        articles = [
            RawArticle(title="短标题", url="https://www.example.com/article"),
            RawArticle(title="这是一个更长的标题", url="https://example.com/article"),
        ]
        merged = deduplicate_by_url(articles)
        assert len(merged) == 1
        assert merged[0].title == "这是一个更长的标题"


# ── 缓存测试 ────────────────────────────────────────────────

class TestCache:
    def test_cache_key(self):
        key1 = _cache_key("https://example.com/article")
        key2 = _cache_key("https://example.com/article")
        assert key1 == key2
        assert len(key1) == 32  # MD5 hex length

    def test_cache_key_different_urls(self):
        key1 = _cache_key("https://example.com/a")
        key2 = _cache_key("https://example.com/b")
        assert key1 != key2
