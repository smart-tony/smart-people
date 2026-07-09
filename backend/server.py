"""
晨间星闻 后端服务
启动: cd backend && uvicorn server:app --host 0.0.0.0 --port 8000
访问: http://localhost:8000/daily
"""
import os
import sys
import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI
from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from routes.llm import router as llm_router
from routes.scrape import router as scrape_router
from routes.briefing import router as briefing_router
from routes.admin import router as admin_router
from settings import get_cors_origins, load_llm_config, require_auth

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="晨间星闻", version="1.0")

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(llm_router)
app.include_router(scrape_router)
app.include_router(briefing_router)
app.include_router(admin_router)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


AUTO_REFRESH_TZ = ZoneInfo(os.getenv("LOGISTICS_AUTO_REFRESH_TZ", "Asia/Shanghai"))
AUTO_REFRESH_INTERVAL_SECONDS = max(300, _env_int("LOGISTICS_AUTO_REFRESH_INTERVAL_SECONDS", 21600))
AUTO_REFRESH_START_HOUR = max(0, min(23, _env_int("LOGISTICS_AUTO_REFRESH_START_HOUR", 6)))
AUTO_REFRESH_END_HOUR = max(0, min(24, _env_int("LOGISTICS_AUTO_REFRESH_END_HOUR", 18)))
LOGISTICS_HEALTH_MAX_AGE_SECONDS = max(3600, _env_int("LOGISTICS_HEALTH_MAX_AGE_SECONDS", 16 * 3600))


def _in_auto_refresh_window(now: datetime) -> bool:
    """是否处于自动刷新时间窗。默认上海时间 06:00 <= now < 18:00。"""
    start = AUTO_REFRESH_START_HOUR
    end = AUTO_REFRESH_END_HOUR
    if start == end:
        return True
    if start < end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end


def _seconds_until_auto_refresh_window(now: datetime) -> int:
    if _in_auto_refresh_window(now):
        return 0

    start_today = now.replace(
        hour=AUTO_REFRESH_START_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if AUTO_REFRESH_START_HOUR < AUTO_REFRESH_END_HOUR:
        target = start_today if now < start_today else start_today + timedelta(days=1)
    else:
        target = start_today
    return max(60, int((target - now).total_seconds()))


@app.on_event("startup")
async def _startup():
    """服务启动：预热缓存 + 启动定时刷新"""
    import asyncio

    async def _warm():
        from routes.briefing import get_briefing, get_logistics
        try:
            await get_briefing(refresh=False)
        except Exception:
            pass
        try:
            await get_logistics(refresh=False)
        except Exception:
            pass

    asyncio.ensure_future(_warm())
    asyncio.ensure_future(_auto_refresh_loop())


async def _auto_refresh_loop():
    """后台定时刷新：默认 06:00-18:00 每 6 小时刷新一次。
    夜间停止主动抓取，降低 token、带宽和来源网站访问压力。
    """
    import asyncio
    from routes.briefing import _schedule_logistics_refresh

    await asyncio.sleep(60)  # 启动后 60 秒再开始，避免和预热冲突

    while True:
        now_local = datetime.now(AUTO_REFRESH_TZ)
        if not _in_auto_refresh_window(now_local):
            await asyncio.sleep(_seconds_until_auto_refresh_window(now_local))
            continue

        try:
            _schedule_logistics_refresh("auto_refresh_loop")
        except Exception:
            pass
        await asyncio.sleep(AUTO_REFRESH_INTERVAL_SECONDS)


@app.get("/api/health")
async def health():
    import time
    from routes.briefing import (
        _cache_logistics,
        _cache_time_logistics,
        _last_logistics_refresh,
        _logistics_refresh_started_at,
        _logistics_refresh_task,
    )

    llm = load_llm_config()
    api_key = llm.get("api_key", "")

    logistics_age = int(time.time() - _cache_time_logistics) if _cache_time_logistics else None
    logistics_items = len(_cache_logistics.get("items", [])) if _cache_logistics else 0
    logistics_healthy = (
        logistics_items > 0
        and logistics_age is not None
        and logistics_age < LOGISTICS_HEALTH_MAX_AGE_SECONDS
    )

    db_today_count = 0
    counts = {}
    try:
        from db import count_by_task_between
        from fastapi.concurrency import run_in_threadpool
        shanghai = ZoneInfo("Asia/Shanghai")
        today_local = datetime.now(shanghai).date()
        start_local = datetime.combine(today_local, datetime.min.time(), tzinfo=shanghai)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        end_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        counts = await run_in_threadpool(count_by_task_between, start_utc, end_utc)
        db_today_count = sum(counts.values()) if counts else 0
    except Exception:
        pass

    return {
        "status": "ok" if logistics_healthy else "degraded",
        "llm_configured": bool(api_key) and api_key != "***",
        "logistics": {
            "healthy": logistics_healthy,
            "cache_items": logistics_items,
            "cache_age_sec": logistics_age,
            "db_today_items": db_today_count,
            "counts_by_task": counts,
            "refresh_running": bool(_logistics_refresh_task and not _logistics_refresh_task.done()),
            "refresh_age_sec": int(time.time() - _logistics_refresh_started_at) if _logistics_refresh_started_at else None,
            "last_refresh": _last_logistics_refresh,
        },
    }


@app.get("/api/img-proxy")
async def img_proxy(url: str = ""):
    """图片代理：绕过微信等平台的防盗链（去掉 Referer）"""
    if not url or not url.startswith("http"):
        from fastapi.responses import Response
        return Response(status_code=400)
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        from fastapi.responses import Response
        return Response(status_code=400)
    if hostname in {"localhost", "0.0.0.0"} or hostname.endswith(".local"):
        from fastapi.responses import Response
        return Response(status_code=403)
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            from fastapi.responses import Response
            return Response(status_code=403)
    except ValueError:
        pass
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer": "",
            })
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "image/jpeg")
            from fastapi.responses import Response
            return Response(content=resp.content, media_type=ct)
    except Exception:
        from fastapi.responses import Response
        return Response(status_code=404)


@app.get("/yunxiaoxing.png")
def serve_mascot():
    for p in _MASCOT_CANDIDATES:
        if p.exists():
            return FileResponse(p, media_type="image/png")
    return FileResponse(STATIC_DIR / "yunxiaoxing.png")


_LOGO_CANDIDATES = [
    STATIC_DIR / "logo.png",
    Path("/Users/z/Desktop/百运网 - LOGO - 全.png"),
    ROOT / "logo.png",
]
_MASCOT_CANDIDATES = [
    STATIC_DIR / "yunxiaoxing.png",
    Path("/Users/z/Desktop/运小星/运小星图片/ChatGPT Image 2026年6月4日 15_42_32.png"),
    ROOT / "yunxiaoxing.png",
]


@app.get("/logo.png")
def serve_logo():
    for p in _LOGO_CANDIDATES:
        if p.exists():
            return FileResponse(p, media_type="image/png")
    svg_path = STATIC_DIR / "logo.svg"
    if svg_path.exists():
        return FileResponse(svg_path, media_type="image/svg+xml")
    return FileResponse(STATIC_DIR / "logo.png")


@app.get("/")
@app.get("/daily")
def serve_daily():
    return FileResponse(ROOT / "daily.html")

@app.get("/briefing")
def serve_briefing():
    return RedirectResponse(url="/daily", status_code=302)

@app.get("/all")
def serve_all():
    return FileResponse(ROOT / "all.html")

@app.get("/admin")
def serve_admin(_user: str | None = Depends(require_auth)):
    return FileResponse(ROOT / "admin.html")


if __name__ == "__main__":
    import uvicorn
    from settings import get_server_host, get_server_port
    print(f"\n  晨间星闻")
    print(f"  🌐 http://localhost:8000/daily\n")
    uvicorn.run(app, host=get_server_host(), port=get_server_port(), log_level="info")
