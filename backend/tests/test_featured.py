import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class FeaturedScoringTest(unittest.TestCase):
    def test_fragment_summary_filtered(self):
        from featured import _is_fragment_summary

        self.assertTrue(_is_fragment_summary("e）正式上线IPI 3.0"))
        self.assertFalse(_is_fragment_summary("美国 CPSC 联合 CBP 启用 PGA eFiling 电子申报。"))

    def test_score_candidate_prefers_customs_theme(self):
        from featured import score_candidate

        item = {
            "title": "注意！美 CPSC 新政给到三个月缓冲期",
            "summary": "CPSC 联合 CBP 启用 PGA eFiling，CSMS 明确三个月缓冲期。",
            "source_url": "https://mjzj.com/article/fry2x444vhts",
            "source_name": "卖家之家",
            "task_type": "crossborder-platform",
            "ai_score": 5.0,
        }
        scored = score_candidate(item)
        self.assertEqual(scored["theme"], "清关合规")
        self.assertGreater(scored["featured_score"], 80)

    def test_sales_noise_excluded_from_featured(self):
        from featured import _passes_hard_filters

        soft = {
            "title": "俄罗斯二手市场迎来爆发！Wildberries 上线货到付款",
            "summary": "WB Resale C2C 二手交易服务升级。",
            "source_url": "https://www.chwang.com/news/207650290546",
            "task_type": "logistics-daily",
        }
        brand = {
            "title": "预判落地：出海硬件价格战开打，白牌先离场｜Hermes品牌信号周报",
            "summary": "Anker 价格战，品牌周报。",
            "source_url": "https://www.cifnews.com/article/123",
            "task_type": "crossborder-platform",
        }
        crime = {
            "title": "世界海关 历时两年调查，巴拿马26名码头工人涉毒被捕！",
            "summary": "缉毒走私团伙可卡因。",
            "source_url": "https://www.5688.cn/news/abc",
            "task_type": "logistics-daily",
        }
        self.assertFalse(_passes_hard_filters(soft))
        self.assertFalse(_passes_hard_filters(brand))
        self.assertFalse(_passes_hard_filters(crime))

    def test_port_disruption_scores_as_sales_priority(self):
        from featured import score_candidate

        item = {
            "title": "霍尔木兹海峡又封了，苏伊士运河通行费即将大涨",
            "summary": "霍尔木兹海峡封锁导致航运受阻，苏伊士通行费上涨。",
            "source_url": "https://www.5688.cn/news/hormuz",
            "source_name": "物流巴巴",
            "task_type": "logistics-daily",
            "ai_score": 5.0,
        }
        scored = score_candidate(item)
        self.assertEqual(scored["theme"], "港口突发")
        self.assertGreaterEqual(scored["featured_priority"], 0)
        self.assertLessEqual(scored["featured_priority"], 1)

    def test_featured_response_falls_back_to_rules(self):
        from featured import get_featured_response

        payload = get_featured_response("2099-01-01")
        self.assertFalse(payload["finalized"])
        self.assertEqual(payload["source"], "rules")
        self.assertLessEqual(payload["total"], 5)

    def test_excluded_tasks_not_in_featured(self):
        from featured import FEATURED_TASKS, EXCLUDED_TASKS, _passes_hard_filters

        for task in ("by56-wiki", "ai-weekly"):
            self.assertNotIn(task, FEATURED_TASKS)
            self.assertIn(task, EXCLUDED_TASKS)
        item = {
            "title": "国际空运扣货怎么办",
            "summary": "百运百科沉淀的清关实操知识。",
            "source_url": "https://www.by56.com/wiki/abc",
            "task_type": "by56-wiki",
        }
        self.assertFalse(_passes_hard_filters(item))

    def test_classify_section_splits_policy_and_industry(self):
        from featured import classify_section

        self.assertEqual(classify_section({"task_type": "policy-official"}), "policy")
        self.assertEqual(classify_section({"task_type": "global-news"}), "policy")
        self.assertEqual(classify_section({"task_type": "logistics-daily"}), "industry")
        self.assertEqual(classify_section({"task_type": "shipping-port"}), "industry")

    def test_llm_unconfigured_falls_back_but_finalized(self):
        import featured
        from featured import _fallback_featured_item

        item = {
            "title": "注意！美 CPSC 新政给到三个月缓冲期！",
            "summary": "CPSC 联合 CBP 启用 PGA eFiling，给予约三个月缓冲期。",
            "source_url": "https://mjzj.com/article/fry2x444vhts",
            "source_name": "卖家之家",
            "task_type": "crossborder-platform",
            "theme": "清关合规",
            "section": "industry",
        }

        orig = featured.load_llm_config
        featured.load_llm_config = lambda: {"api_key": "", "model": "deepseek-chat", "max_tokens": 700}
        try:
            out = featured.format_featured_item_with_llm(item)
        finally:
            featured.load_llm_config = orig

        self.assertTrue(out["finalized"])
        self.assertFalse(out["llm_ok"])
        self.assertIn("llm_error", out)

        manual = _fallback_featured_item(item)
        self.assertTrue(manual["finalized"])
        self.assertFalse(manual["llm_ok"])
        self.assertEqual(manual["impact"], "影响需结合原文人工确认。")

    def test_auto_format_skips_manual_finalized(self):
        import featured

        day = "2099-02-02"
        store = {
            "date": day,
            "finalized": True,
            "source": "manual",
            "items": [{"title": "人工定稿", "source_url": "https://example.com/a"}],
        }
        orig_load = featured.load_featured_store
        orig_build = featured.build_featured_candidates
        orig_finalize = featured.finalize_featured
        featured.load_featured_store = lambda date=None: store
        featured.build_featured_candidates = lambda date=None, limit=None: (_ for _ in ()).throw(
            AssertionError("should not build when manual")
        )
        featured.finalize_featured = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not finalize when manual")
        )
        try:
            result = featured.auto_format_featured(day)
        finally:
            featured.load_featured_store = orig_load
            featured.build_featured_candidates = orig_build
            featured.finalize_featured = orig_finalize
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("skip_reason"), "manual_finalized")

    def test_auto_format_calls_finalize_with_source_auto(self):
        import featured

        day = "2099-02-03"
        captured = {}

        def fake_build(date=None, limit=None):
            return [
                {"source_url": "https://example.com/1", "title": "港口停航"},
                {"source_url": "https://example.com/2", "title": "运价上涨"},
            ]

        def fake_finalize(urls, date=None, use_llm=True, source="manual"):
            captured["urls"] = urls
            captured["date"] = date
            captured["use_llm"] = use_llm
            captured["source"] = source
            return {
                "date": date,
                "finalized": True,
                "source": source,
                "items": [{"title": "x", "llm_ok": True}],
                "missing_urls": [],
            }

        orig_load = featured.load_featured_store
        orig_build = featured.build_featured_candidates
        orig_finalize = featured.finalize_featured
        featured.load_featured_store = lambda date=None: None
        featured.build_featured_candidates = fake_build
        featured.finalize_featured = fake_finalize
        try:
            result = featured.auto_format_featured(day)
        finally:
            featured.load_featured_store = orig_load
            featured.build_featured_candidates = orig_build
            featured.finalize_featured = orig_finalize

        self.assertEqual(captured["source"], "auto")
        self.assertTrue(captured["use_llm"])
        self.assertEqual(captured["date"], day)
        self.assertEqual(len(captured["urls"]), 2)
        self.assertEqual(result["source"], "auto")

    def test_finalize_featured_marks_missing_and_skips_excluded(self):
        import featured

        mjzj_url = "https://mjzj.com/article/fry2x444vhts"
        mjzj_item = {
            "title": "注意！美 CPSC 新政给到三个月缓冲期！",
            "summary": "CPSC 联合 CBP 启用 PGA eFiling，CSMS 公告明确三个月缓冲期执行细则。",
            "source_url": mjzj_url,
            "source_name": "卖家之家",
            "task_type": "crossborder-platform",
            "ai_score": 5.0,
        }

        def fake_lookup(urls, date=None):
            return {mjzj_url: mjzj_item}

        def fake_format(item):
            return featured._fallback_featured_item(item, llm_error="test-no-llm")

        def fake_passes(item, weights=None):
            # 只放行行业+政策候选，百科 url 模拟被硬过滤排除
            url = item.get("source_url", "")
            if "by56.com/wiki" in url:
                return False
            return True

        orig_lookup = featured._lookup_today_items_by_urls
        orig_format = featured.format_featured_item_with_llm
        orig_save = featured.save_featured_store
        orig_passes = featured._passes_hard_filters
        featured._lookup_today_items_by_urls = fake_lookup
        featured.format_featured_item_with_llm = fake_format
        featured._passes_hard_filters = fake_passes
        featured.save_featured_store = lambda payload, date=None: payload
        try:
            result = featured.finalize_featured(
                [
                    mjzj_url,
                    "https://example.com/missing",
                    "https://www.by56.com/wiki/abc",
                ],
                date="2099-01-01",
                use_llm=True,
            )
        finally:
            featured._lookup_today_items_by_urls = orig_lookup
            featured.format_featured_item_with_llm = orig_format
            featured._passes_hard_filters = orig_passes
            featured.save_featured_store = orig_save

        self.assertEqual(result["date"], "2099-01-01")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["source_url"], mjzj_url)
        self.assertIn("https://example.com/missing", result["missing_urls"])
        self.assertIn("https://www.by56.com/wiki/abc", result["missing_urls"])
