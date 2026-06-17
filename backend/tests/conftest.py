"""
weekly-push-tool 测试共用 fixture
"""
import json
import sys
from pathlib import Path

import pytest

# 确保 backend 在 sys.path 中
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

ROOT_DIR = BACKEND_DIR.parent
CONFIG_DIR = ROOT_DIR / "config"


@pytest.fixture
def sources_config():
    """加载 sources.config.json"""
    path = CONFIG_DIR / "sources.config.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


@pytest.fixture
def prompts_config():
    """加载 prompts.config.json"""
    path = CONFIG_DIR / "prompts.config.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}
