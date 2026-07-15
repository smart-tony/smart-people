import sys
import unittest
import os
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class BriefingShapeTest(unittest.TestCase):
    def test_normalize_logistics_payload_supports_page_fields(self):
        from routes.briefing import _normalize_logistics_payload

        payload = _normalize_logistics_payload(
            {
                "sources": [{"label": "物流资讯", "task_type": "logistics-daily", "count": 1}],
                "items": [
                    {
                        "title": "  1秒前 TikTok升级AI营销工具 分享至  ",
                        "source_name": "出海网快讯",
                        "source_url": "https://www.chwang.com/news/123",
                        "ai_summary": "平台升级 AI 营销工具，可能影响跨境商家的投放效率、订单转化和履约节奏，需要关注后续规则变化。",
                    }
                ],
            },
            default_task="logistics-daily",
            default_label="物流资讯",
        )

        item = payload["items"][0]
        self.assertEqual(item["task"], "logistics-daily")
        self.assertEqual(item["label"], "物流资讯")
        self.assertEqual(item["summary"], "平台升级 AI 营销工具，可能影响跨境商家的投放效率、订单转化和履约节奏，需要关注后续规则变化。")
        self.assertEqual(item["ai_summary"], "平台升级 AI 营销工具，可能影响跨境商家的投放效率、订单转化和履约节奏，需要关注后续规则变化。")
        self.assertEqual(item["title"], "TikTok升级AI营销工具")
        self.assertIn("updated_at", payload)

    def test_local_date_bounds_cover_shanghai_morning_scrape(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        from settings import local_date_bounds

        sh = ZoneInfo("Asia/Shanghai")
        # 上海 2026-07-10 06:30 = UTC 2026-07-09 22:30，应归入上海「7月10日」
        sample = datetime(2026, 7, 9, 22, 30, 0, tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        start, end = local_date_bounds("2026-07-10")
        self.assertLessEqual(start, sample)
        self.assertLess(sample, end)

        # 上海 2026-07-10 07:59 仍在同一天
        sample2 = datetime(2026, 7, 9, 23, 59, 0, tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.assertLess(sample2, end)

        # 上海 2026-07-10 08:00 = UTC 2026-07-10 00:00
        day_start_utc = datetime(2026, 7, 10, 0, 0, 0, tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(start, day_start_utc)
        self.assertLess(day_start_utc, end)

    def test_beijing_refresh_schedule_slots(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from settings import allow_page_stale_refresh, next_scheduled_refresh

        sh = ZoneInfo("Asia/Shanghai")
        before = datetime(2026, 7, 13, 8, 0, tzinfo=sh)
        self.assertEqual(next_scheduled_refresh(before).strftime("%H:%M"), "08:30")
        mid = datetime(2026, 7, 13, 9, 0, tzinfo=sh)
        self.assertEqual(next_scheduled_refresh(mid).strftime("%H:%M"), "10:00")
        after_noon = datetime(2026, 7, 13, 12, 0, tzinfo=sh)
        self.assertEqual(next_scheduled_refresh(after_noon).strftime("%H:%M"), "14:00")
        evening = datetime(2026, 7, 13, 18, 0, tzinfo=sh)
        nxt = next_scheduled_refresh(evening)
        self.assertEqual(nxt.strftime("%Y-%m-%d %H:%M"), "2026-07-14 08:30")
        self.assertFalse(allow_page_stale_refresh(evening))
        self.assertTrue(allow_page_stale_refresh(datetime(2026, 7, 13, 10, 30, tzinfo=sh)))
        self.assertFalse(allow_page_stale_refresh(datetime(2026, 7, 13, 8, 29, tzinfo=sh)))
        self.assertTrue(allow_page_stale_refresh(datetime(2026, 7, 13, 15, 0, tzinfo=sh)))
        self.assertFalse(allow_page_stale_refresh(datetime(2026, 7, 13, 15, 1, tzinfo=sh)))

    def test_resolve_query_date_rejects_invalid(self):
        from settings import resolve_query_date

        self.assertEqual(resolve_query_date("2026-07-13"), "2026-07-13")
        self.assertEqual(resolve_query_date(" 2026-07-13 "), "2026-07-13")
        self.assertEqual(resolve_query_date("2026-13-40", default="2099-01-01"), "2099-01-01")
        self.assertEqual(resolve_query_date("not-a-date", default="2099-01-01"), "2099-01-01")
        self.assertEqual(resolve_query_date("", default="2099-01-01"), "2099-01-01")

    def test_auto_refresh_loop_source_has_no_undefined_names(self):
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        # 确保循环体内不再引用未导入的 next_scheduled_refresh
        loop_fn = next(
            n for n in tree.body
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "_auto_refresh_loop"
        )
        names = {node.id for node in ast.walk(loop_fn) if isinstance(node, ast.Name)}
        self.assertNotIn("next_scheduled_refresh", names)
        self.assertIn("seconds_until_next_refresh", names)


class ExtractorNoiseTest(unittest.TestCase):
    def test_extract_publish_date_prefers_raw_html_date_nodes(self):
        from routes.scrape import _extract_publish_date

        html = """
        <html>
          <head>
            <meta property="og:image" content="https://res.by56.com/upload/News/2026/7/image.png">
          </head>
          <body>
            <div class="detail-date"><span>2026年07月09日 17:13:33 更新</span></div>
            <article>正文里没有标准发布时间。</article>
          </body>
        </html>
        """

        self.assertEqual(_extract_publish_date(html), "2026-07-09")

    def test_long_body_compact_trigger_uses_body_without_publish_date(self):
        from models import RawArticle
        from routes.scrape import _should_compact_long_body_with_llm

        old_enabled = os.environ.get("COMPACT_LONG_BODY_WITH_LLM")
        old_min_chars = os.environ.get("COMPACT_LONG_BODY_MIN_CHARS")
        try:
            os.environ["COMPACT_LONG_BODY_WITH_LLM"] = "true"
            os.environ["COMPACT_LONG_BODY_MIN_CHARS"] = "800"
            long_article = RawArticle(
                title="长正文测试",
                url="https://example.com/a",
                content_snippet="[发布日期: 2026-07-09]\n\n" + "这是一段正文。" * 120,
            )
            short_article = RawArticle(
                title="短正文测试",
                url="https://example.com/b",
                content_snippet="[发布日期: 2026-07-09]\n\n短正文。",
            )

            self.assertTrue(_should_compact_long_body_with_llm(long_article, "logistics-daily"))
            self.assertFalse(_should_compact_long_body_with_llm(short_article, "logistics-daily"))
        finally:
            if old_enabled is None:
                os.environ.pop("COMPACT_LONG_BODY_WITH_LLM", None)
            else:
                os.environ["COMPACT_LONG_BODY_WITH_LLM"] = old_enabled
            if old_min_chars is None:
                os.environ.pop("COMPACT_LONG_BODY_MIN_CHARS", None)
            else:
                os.environ["COMPACT_LONG_BODY_MIN_CHARS"] = old_min_chars

    def test_logistics_extractors_filter_common_noise(self):
        from bs4 import BeautifulSoup
        from routes.scrape import _extract_articles_from_html

        wl123 = _extract_articles_from_html(
            {"id": "wl123_news", "name": "WL123", "url": "https://www.wl123.com/wu-liu-zi-xun"},
            """
            <a href="/company/fundpark">丰泊国际FundPark</a>
            <a href="/">WL123跨境物流导航生态资源服务平台 首页</a>
            <a href="/wu-liu-zi-xun/123.html">港口拥堵影响美线时效</a>
            """,
            "https://www.wl123.com/wu-liu-zi-xun",
            5,
        )
        self.assertEqual([a.title for a in wl123], ["港口拥堵影响美线时效"])

        cifnews = _extract_articles_from_html(
            {"id": "cifnews", "name": "雨果跨境", "url": "https://www.cifnews.com/"},
            """
            <a href="/product/2">亚马逊开店 一键开通18站点</a>
            <a href="/article/12345">美国FBA入仓预约规则更新</a>
            """,
            "https://www.cifnews.com/",
            5,
        )
        self.assertEqual([a.title for a in cifnews], ["美国FBA入仓预约规则更新"])

        ship = _extract_articles_from_html(
            {"id": "ship_sh", "name": "航运界", "url": "https://www.ship.sh/"},
            """
            <a href="mailto:editor@ship.sh">editor@ship.sh</a>
            <a href="/articles/red-sea">红海航线风险再次升温</a>
            """,
            "https://www.ship.sh/",
            5,
        )
        self.assertEqual([a.title for a in ship], ["红海航线风险再次升温"])

    def test_strip_5688_reading_meta_noise(self):
        from routes.scrape import strip_reading_meta, _clean_feed_title
        from routes.briefing import _clean_common_summary

        dirty = (
            "美西线现货合约配售价一周内重挫36.67%。市场... "
            "4 分钟阅读 · 2026-07-14 · 15 阅读 国际海运 海运运价 运价拐点初现 "
            "近日 SCFI 指数下跌，四大航线运价集体回落。"
        )
        cleaned = strip_reading_meta(dirty)
        self.assertNotIn("分钟阅读", cleaned)
        self.assertNotIn("15 阅读", cleaned)
        self.assertNotIn("国际海运", cleaned)
        self.assertIn("SCFI", cleaned)
        self.assertIn("近日", cleaned)

        via_summary = _clean_common_summary(dirty, "运价拐点初现")
        self.assertNotIn("分钟阅读", via_summary)
        self.assertNotIn("15 阅读", via_summary)
        self.assertIn("SCFI", via_summary)

        self.assertEqual(_clean_feed_title("海运新闻 运价拐点初现"), "运价拐点初现")
        self.assertEqual(_clean_feed_title("世界海关 CBP 清关新规"), "CBP 清关新规")


if __name__ == "__main__":
    unittest.main()
