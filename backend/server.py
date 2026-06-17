"""
weekly-push-tool 后端服务
Phase 1: 骨架 — 静态文件服务 + 健康检查 + 配置加载
启动: cd backend && python server.py
访问: http://localhost:8000
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from routes.llm import router as llm_router
from routes.scrape import router as scrape_router
from routes.workspace import router as workspace_router
from settings import get_cors_origins, get_server_host, get_server_port, load_llm_config, require_auth

# ── 路径常量 ──────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent          # weekly-push-tool/
CONFIG_DIR = ROOT / "config"
TEMPLATES_DIR = ROOT / "templates"

# ── 配置加载 ──────────────────────────────────────────────
def load_config(filename: str, default=None) -> dict:
    """加载 config/ 下的 JSON 文件，不存在则返回默认值"""
    path = CONFIG_DIR / filename
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}


def count_sources(sources_config: dict) -> int:
    """统计来源配置数量，兼容按分类分组的结构。"""
    sources_val = sources_config.get("sources")
    if isinstance(sources_val, list):
        return len(sources_val)
    if isinstance(sources_val, dict):
        return sum(len(v) for v in sources_val.values() if isinstance(v, list))
    return 0

# ── 应用实例 ──────────────────────────────────────────────
app = FastAPI(
    title="Weekly Push Tool API",
    version="0.1.0",
    description="百运科技 · 内容推送工作台后端"
)

# CORS — 允许前端跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(llm_router)
app.include_router(scrape_router)
app.include_router(workspace_router)

# ── API 路由 ──────────────────────────────────────────────

@app.get("/api/health")
def health():
    """健康检查 + 显示已加载的配置状态"""
    llm_config = load_llm_config()
    sources_config = load_config("sources.config.json")
    api_key = llm_config.get("api_key", "")
    llm_ok = bool(api_key) and api_key != "***"

    return {
        "status": "ok",
        "version": "0.1.0",
        "llm_configured": llm_ok,
        "sources_count": count_sources(sources_config),
    }


@app.get("/api/config")
def get_config():
    """查看所有配置（脱敏）"""
    llm = load_config("llm.config.json")
    sources = load_config("sources.config.json")

    # 脱敏 API key
    if "api_key" in llm:
        key = llm["api_key"]
        llm["api_key"] = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"

    return {
        "llm": llm,
        "sources": sources,
    }


@app.post("/api/config/sources")
def save_sources_config(payload: dict, username: str | None = Depends(require_auth)):
    """保存来源配置到 sources.config.json（需要认证，如已配置 APP_USERNAME）"""
    if not isinstance(payload, dict):
        raise HTTPException(400, "来源配置必须是 JSON 对象")
    path = CONFIG_DIR / "sources.config.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"success": True, "path": str(path)}


# ── 静态文件（放在最后，避免覆盖 API 路由） ──────────────

@app.get("/")
def serve_index():
    return FileResponse(ROOT / "index.html")


# 挂载根目录的静态资源
app.mount("/", StaticFiles(directory=str(ROOT)), name="static")


# ── 直接运行入口 ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print(f"\n  🔧 weekly-push-tool backend")
    print(f"  📂 {ROOT}")
    print(f"  🌐 http://localhost:8000")
    print(f"  📋 http://localhost:8000/docs  (API 文档)\n")
    uvicorn.run(app, host=get_server_host(), port=get_server_port(), log_level="info")
