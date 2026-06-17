import json
import os
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def load_config(filename: str, default=None) -> dict:
    path = CONFIG_DIR / filename
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def load_llm_config() -> dict:
    config = load_config("llm.config.json", {})

    if os.getenv("LLM_PROVIDER"):
        config["provider"] = os.getenv("LLM_PROVIDER")
    if os.getenv("LLM_API_KEY"):
        config["api_key"] = os.getenv("LLM_API_KEY")
    if os.getenv("LLM_BASE_URL"):
        config["base_url"] = os.getenv("LLM_BASE_URL")
    if os.getenv("LLM_MODEL"):
        config["model"] = os.getenv("LLM_MODEL")
    if os.getenv("LLM_MAX_TOKENS"):
        config["max_tokens"] = _env_int("LLM_MAX_TOKENS", config.get("max_tokens", 2048))
    if os.getenv("LLM_TEMPERATURE"):
        config["temperature"] = _env_float("LLM_TEMPERATURE", config.get("temperature", 0.3))

    return config


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_basic_auth() -> tuple[str, str] | None:
    username = os.getenv("APP_USERNAME", "").strip()
    password = os.getenv("APP_PASSWORD", "")
    if username and password:
        return username, password
    return None


def get_server_host() -> str:
    return os.getenv("APP_HOST", "0.0.0.0")


def get_server_port() -> int:
    return _env_int("PORT", 8000)


# ── 认证依赖 ─────────────────────────────────────────────

_security = HTTPBasic(auto_error=False)


async def require_auth(credentials: HTTPBasicCredentials | None = Depends(_security)):
    """HTTP Basic Auth 依赖。当 APP_USERNAME / APP_PASSWORD 未设置时，路由开放无需认证。"""
    auth = get_basic_auth()
    if auth is None:
        return None  # 未配置账号密码，放行

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要认证：请设置 APP_USERNAME / APP_PASSWORD 环境变量",
            headers={"WWW-Authenticate": "Basic"},
        )

    username_ok = secrets.compare_digest(credentials.username.encode(), auth[0].encode())
    password_ok = secrets.compare_digest(credentials.password.encode(), auth[1].encode())
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
