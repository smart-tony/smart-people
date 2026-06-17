"""
llm.py 功能测试
"""
import pytest

from routes.llm import VALID_TASK_TYPES, TASK_TYPE_ALIASES
from routes.scrape import _extract_tags


# ── VALID_TASK_TYPES 测试 ───────────────────────────────────

class TestValidTaskTypes:
    def test_standard_types_present(self):
        for t in ("ai-weekly", "global-logistics-risk", "cn-logistics-industry", "exchange-rate"):
            assert t in VALID_TASK_TYPES

    def test_aliases_present(self):
        """别名也应在 VALID_TASK_TYPES 中，这样校验不会拒绝"""
        for alias in TASK_TYPE_ALIASES:
            assert alias in VALID_TASK_TYPES

    def test_aliases_map_to_valid_types(self):
        """别名映射的目标类型必须是标准类型"""
        for alias, target in TASK_TYPE_ALIASES.items():
            assert target in VALID_TASK_TYPES
            assert target != alias  # 别名不应该映射到自身


# ── Prompt 覆盖测试 ────────────────────────────────────────

class TestPromptCoverage:
    def test_all_source_modules_have_prompt(self, sources_config, prompts_config):
        """确保每个 sources.config 模块通过别名或直接都能找到对应的 prompt"""
        source_keys = set(sources_config.get("sources", {}).keys())
        prompt_keys = set(k for k in prompts_config.keys() if not k.startswith("_"))

        for module_key in source_keys:
            # 直接匹配或通过别名匹配
            resolved = TASK_TYPE_ALIASES.get(module_key, module_key)
            assert resolved in prompt_keys, (
                f"模块 '{module_key}' (解析为 '{resolved}') 没有对应的 prompt。"
                f"可用 prompt: {prompt_keys}"
            )


# ── _extract_tags 测试 ──────────────────────────────────────

class TestExtractTags:
    def test_list_tags(self):
        assert _extract_tags({"tags": ["AI", "LLM", "Agent"]}) == ["AI", "LLM", "Agent"]

    def test_string_tags(self):
        assert _extract_tags({"tags": "AI, LLM, Agent"}) == ["AI", "LLM", "Agent"]

    def test_empty_tags(self):
        assert _extract_tags({"tags": []}) == []
        assert _extract_tags({"tags": ""}) == []

    def test_missing_tags(self):
        assert _extract_tags({}) == []

    def test_nonstandard_tags(self):
        """tags 字段为数字等其他类型时不应崩溃"""
        result = _extract_tags({"tags": 123})
        assert result == []
