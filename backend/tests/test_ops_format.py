import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class OpsFormatTest(unittest.TestCase):
    def test_auto_format_skips_when_llm_unconfigured(self):
        import ops_format

        orig = ops_format.load_llm_config
        ops_format.load_llm_config = lambda: {"api_key": "", "model": "deepseek-chat"}
        try:
            result = ops_format.auto_format_ops_items("2099-03-01")
        finally:
            ops_format.load_llm_config = orig
        self.assertTrue(result["skipped"])
        self.assertEqual(result["skip_reason"], "llm_unconfigured")

    def test_auto_format_processes_pending_once(self):
        import ops_format

        pending = [
            {
                "title": "美西运价下跌",
                "summary": "SCFI 回调",
                "body_text": "最新一期 SCFI 指数下跌，美西线运价回落。",
                "source_url": "https://example.com/ops-1",
                "source_name": "测试源",
                "task_type": "logistics-daily",
                "analysis": "",
            }
        ]
        updates = []

        def fake_list(**kwargs):
            return list(pending)

        def fake_update(url, **kwargs):
            updates.append({"url": url, **kwargs})

        def fake_format(item):
            return {
                "title": "美西运价下跌",
                "summary": "SCFI 指数回调，美西线运价下行。",
                "analysis": "出货报价可参考最新现货，注意舱位波动。",
                "llm_ok": True,
                "llm_error": "",
            }

        orig_load = ops_format.load_llm_config
        orig_list = ops_format.list_unformatted_ops_items
        orig_update = ops_format.update_item_llm_format
        orig_format = ops_format.format_ops_item_with_llm
        ops_format.load_llm_config = lambda: {
            "api_key": "sk-test",
            "model": "deepseek-chat",
            "max_tokens": 700,
        }
        ops_format.list_unformatted_ops_items = fake_list
        ops_format.update_item_llm_format = fake_update
        ops_format.format_ops_item_with_llm = fake_format
        try:
            result = ops_format.auto_format_ops_items("2099-03-02")
        finally:
            ops_format.load_llm_config = orig_load
            ops_format.list_unformatted_ops_items = orig_list
            ops_format.update_item_llm_format = orig_update
            ops_format.format_ops_item_with_llm = orig_format

        self.assertFalse(result["skipped"])
        self.assertEqual(result["ok"], 1)
        self.assertEqual(len(updates), 1)
        self.assertIn("出货报价", updates[0]["analysis"])

    def test_auto_format_skips_when_none_pending(self):
        import ops_format

        orig_load = ops_format.load_llm_config
        orig_list = ops_format.list_unformatted_ops_items
        ops_format.load_llm_config = lambda: {"api_key": "sk-test", "model": "deepseek-chat"}
        ops_format.list_unformatted_ops_items = lambda **kwargs: []
        try:
            result = ops_format.auto_format_ops_items("2099-03-03")
        finally:
            ops_format.load_llm_config = orig_load
            ops_format.list_unformatted_ops_items = orig_list
        self.assertTrue(result["skipped"])
        self.assertEqual(result["skip_reason"], "none_pending")


if __name__ == "__main__":
    unittest.main()
