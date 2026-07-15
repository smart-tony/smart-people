import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
_DATA_DIR: Path | None = None


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def get_data_dir() -> Path:
    """返回可写的数据目录。

    优先级：
    1. APP_DATA_DIR 环境变量
    2. 项目内 data/
    3. ~/Library/Application Support/weekly-push-tool/data
    """
    global _DATA_DIR
    if _DATA_DIR is not None:
        return _DATA_DIR

    candidates: list[Path] = []
    env_dir = os.getenv("APP_DATA_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    candidates.append(ROOT / "data")
    candidates.append(Path.home() / "Library" / "Application Support" / "weekly-push-tool" / "data")

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if _is_writable_dir(resolved):
            if resolved != (ROOT / "data").resolve():
                print(f"[settings] 使用数据目录: {resolved}")
            _DATA_DIR = resolved
            return _DATA_DIR

    fallback = (ROOT / "data").resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    _DATA_DIR = fallback
    return _DATA_DIR


def _load_dotenv_file():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        print(f"[settings] .env 读取失败：{exc}")


_load_dotenv_file()


def load_config(filename: str, default=None) -> dict:
    """加载 config/ 下的 JSON 文件。

    容错：文件不存在或损坏（半截 JSON）时回退到默认值，避免配置损坏导致整个服务启动失败。
    """
    path = CONFIG_DIR / filename
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[settings] 配置文件 {filename} 读取失败，使用默认值：{exc}")
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

    # DEEPSEEK_API_KEY 作为 LLM_API_KEY 的备选，优先级：LLM_API_KEY > DEEPSEEK_API_KEY > 配置文件
    if os.getenv("LLM_API_KEY"):
        config["api_key"] = os.getenv("LLM_API_KEY")
    elif os.getenv("DEEPSEEK_API_KEY"):
        config["api_key"] = os.getenv("DEEPSEEK_API_KEY")
    if os.getenv("LLM_BASE_URL"):
        config["base_url"] = os.getenv("LLM_BASE_URL")
    if os.getenv("LLM_MODEL"):
        config["model"] = os.getenv("LLM_MODEL")
    if os.getenv("LLM_MAX_TOKENS"):
        config["max_tokens"] = _env_int("LLM_MAX_TOKENS", config.get("max_tokens", 2048))
    if os.getenv("LLM_TEMPERATURE"):
        config["temperature"] = _env_float("LLM_TEMPERATURE", config.get("temperature", 0.3))

    return config


def load_publish_config() -> dict:
    """加载发布配置，敏感字段（admin_password 等）优先从环境变量读取。"""
    config = load_config("publish.config.json", {})

    if os.getenv("PUBLISH_ADMIN_BASE_URL"):
        config["admin_base_url"] = os.getenv("PUBLISH_ADMIN_BASE_URL")
    if os.getenv("PUBLISH_ADMIN_USER"):
        config["admin_user"] = os.getenv("PUBLISH_ADMIN_USER")
    if os.getenv("PUBLISH_ADMIN_PASSWORD"):
        config["admin_password"] = os.getenv("PUBLISH_ADMIN_PASSWORD")
    if os.getenv("PUBLISH_DEFAULT_USER_ID"):
        config["default_user_id"] = os.getenv("PUBLISH_DEFAULT_USER_ID")

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


def get_app_tz() -> ZoneInfo:
    """业务日界线时区，与自动刷新窗口一致。默认 Asia/Shanghai（北京时间）。"""
    return ZoneInfo(os.getenv("LOGISTICS_AUTO_REFRESH_TZ", "Asia/Shanghai"))


def get_auto_refresh_times() -> list[tuple[int, int]]:
    """自动抓取时刻表（北京时间），默认 08:30 / 10:00 / 14:00。

    环境变量 LOGISTICS_AUTO_REFRESH_TIMES=08:30,10:00,14:00
    """
    raw = os.getenv("LOGISTICS_AUTO_REFRESH_TIMES", "08:30,10:00,14:00").strip()
    parsed: list[tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":", 1)
            hour = int(hh)
            minute = int(mm)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                parsed.append((hour, minute))
        except ValueError:
            continue
    if not parsed:
        return [(8, 30), (10, 0), (14, 0)]
    return sorted(set(parsed))


def next_scheduled_refresh(now: datetime | None = None) -> datetime:
    """返回下一个抓取时刻（带时区）。"""
    tz = get_app_tz()
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    for hour, minute in get_auto_refresh_times():
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            return candidate
    first_h, first_m = get_auto_refresh_times()[0]
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=first_h, minute=first_m, second=0, microsecond=0)


def seconds_until_next_refresh(now: datetime | None = None) -> int:
    target = next_scheduled_refresh(now)
    now = now or datetime.now(get_app_tz())
    if now.tzinfo is None:
        now = now.replace(tzinfo=get_app_tz())
    return max(30, int((target - now).total_seconds()))


def allow_page_stale_refresh(now: datetime | None = None) -> bool:
    """页面过期补抓是否允许。首档到末档后 1 小时内允许，晚上不允许。"""
    tz = get_app_tz()
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    times = get_auto_refresh_times()
    first_h, first_m = times[0]
    last_h, last_m = times[-1]
    start = now.replace(hour=first_h, minute=first_m, second=0, microsecond=0)
    end = now.replace(hour=last_h, minute=last_m, second=0, microsecond=0) + timedelta(hours=1)
    return start <= now <= end


_QUERY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_query_date(date: str | None = "", default: str | None = None) -> str:
    """校验并返回 YYYY-MM-DD 查询日期；无效或空则回退 default 或 today_local()。"""
    day = (date or "").strip()
    if day and _QUERY_DATE_RE.fullmatch(day):
        try:
            datetime.strptime(day, "%Y-%m-%d")
            return day
        except ValueError:
            pass
    return default or today_local()


def today_local() -> str:
    """当前业务时区的日期 YYYY-MM-DD（用于「今天」判定）。"""
    return datetime.now(get_app_tz()).strftime("%Y-%m-%d")


def local_date_bounds(date_str: str | None = None) -> tuple[str, str]:
    """将业务时区某一天映射为 scraped_at（UTC 存储）的查询区间 [start, end)。

    scraped_at 以 UTC 写入；按上海「今天」查库时需用本地 0 点对应的 UTC 边界。
    """
    tz = get_app_tz()
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        day = datetime.now(tz).date()
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return start_utc, end_utc


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
