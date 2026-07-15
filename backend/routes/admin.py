"""
管理后台路由 — 手动触发晨间星闻行业/政策抓取 → SQLite 数据库
POST /api/admin/scrape/{module}    → 抓取单个模块，写入数据库
POST /api/admin/scrape-all          → 抓取全部模块（LLM 排版后台异步）
POST /api/admin/reformat            → 强制重新 DeepSeek 排版当日行业/政策（故障兜底）
POST /api/admin/publish-all         → 一键发布今天候选
GET  /api/admin/status              → 查看数据库状态
"""
import json
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from db import cleanup_old_items, insert_items, get_stats, publish_all_today
from settings import get_data_dir, require_auth

from routes.scrape import (
    pick_sources,
    fetch_raw_articles,
    deduplicate_by_url,
    enrich_article_content,
    analyze_with_llm,
    filter_recent_articles,
    filter_and_sort,
    resolve_task_type,
)

router = APIRouter(prefix="/api/admin", tags=["晨间星闻管理后台"], dependencies=[Depends(require_auth)])

ROOT = Path(__file__).resolve().parent.parent

LOGISTICS_TASKS = [
    ("logistics-daily",      "行业动态｜物流资讯"),
    ("crossborder-platform", "行业动态｜跨境平台"),
    ("shipping-port",        "行业动态｜航运港口"),
    ("by56-wiki",            "行业动态｜百运百科"),
    ("global-news",          "政策动态｜地缘与国际风险"),
    ("policy-official",      "政策动态｜关税与官方政策"),
]
TASK_LABELS = dict(LOGISTICS_TASKS)

# 各模块时效配置（天数）
MODULE_RECENCY = {"by56-wiki": 2, "policy-official": 14, "global-news": 7}
DEFAULT_RECENCY = 5

# 各模块评分门槛
MODULE_THRESHOLD = {"by56-wiki": 0, "policy-official": 4, "global-news": 4}
DEFAULT_THRESHOLD = 5

# 各模块抓取条数上限
MODULE_LIMIT = {"by56-wiki": 5, "policy-official": 8, "global-news": 8}
DEFAULT_LIMIT = 10


async def _scrape_one(task_type: str, label: str) -> dict:
    """抓取单个模块 → 写入 SQLite"""
    try:
        sources = pick_sources(task_type, [])
        if not sources:
            return {"label": label, "task_type": task_type, "count": 0, "ok": False,
                    "error": "未找到可用来源"}

        raw_articles, fetch_errors = await fetch_raw_articles(sources, limit=MODULE_LIMIT.get(task_type, DEFAULT_LIMIT))
        unique_articles = deduplicate_by_url(raw_articles)
        unique_articles = await enrich_article_content(unique_articles)

        recency = MODULE_RECENCY.get(task_type, DEFAULT_RECENCY)
        unique_articles, _ = filter_recent_articles(unique_articles, recency_days=recency)

        resolved_task = resolve_task_type(task_type)
        candidates, llm_errors, tokens = await run_in_threadpool(
            analyze_with_llm, unique_articles, resolved_task
        )

        threshold = MODULE_THRESHOLD.get(task_type, DEFAULT_THRESHOLD)
        candidates = filter_and_sort(candidates, threshold=threshold)

        # 转换为数据库格式
        items = []
        for c in candidates:
            item = c.model_dump(mode="json") if hasattr(c, 'model_dump') else dict(c)
            items.append({
                "title": item.get("title", ""),
                "source_name": item.get("source_name", item.get("source_id", "")),
                "source_url": item.get("source_url", item.get("url", "")),
                "summary": item.get("ai_summary", ""),
                "analysis": item.get("ai_analysis", ""),
                "body_text": item.get("body_text", ""),
                "ai_score": item.get("ai_score", 5.0),
                "ai_tags": item.get("ai_tags", []),
                "task_type": task_type,
            })

        inserted = insert_items(items)

        return {
            "label": label, "task_type": task_type,
            "count": inserted, "total_candidates": len(candidates), "ok": True,
        }
    except Exception as e:
        return {"label": label, "task_type": task_type, "count": 0, "ok": False,
                "error": str(e)}


@router.get("/status")
def admin_status():
    """查看数据库状态"""
    stats = get_stats()
    return {"ok": True, **stats}


@router.post("/scrape/{module}")
async def scrape_module(module: str):
    """抓取单个模块 → 写入数据库"""
    pair = next(((t, l) for t, l in LOGISTICS_TASKS if t == module), None)
    if not pair:
        return {"ok": False, "error": f"未知模块: {module}"}
    result = await _scrape_one(*pair)
    return {
        "ok": result["ok"],
        "module": module,
        "count": result["count"],
        "total_candidates": result.get("total_candidates", 0),
    }


@router.post("/scrape-all")
async def scrape_all():
    """抓取全部模块 → 写入数据库"""
    results = []
    for task_type, label in LOGISTICS_TASKS:
        r = await _scrape_one(task_type, label)
        results.append(r)
        await asyncio.sleep(1)

    total = sum(r["count"] for r in results)
    # 抓取入库后，LLM 排版走后台非阻塞，避免管理端请求长时间挂起
    try:
        from routes.briefing import _schedule_featured_auto_format
        from settings import today_local

        _schedule_featured_auto_format(today_local(), "admin_scrape_all")
    except Exception:
        pass
    return {
        "ok": True,
        "total_inserted": total,
        "sources": results,
        "formatting": "scheduled_in_background",
    }


@router.post("/reformat")
async def reformat_ops(force: bool = True, date: str = ""):
    """强制重新 DeepSeek 排版行业/政策条目（DeepSeek 故障窗口兜底）。

    默认 force=True：忽略 llm_formatted_at 标记，对当日行业/政策全量重排。
    精选保持人工定稿优先；未人工定稿时也会重排。
    """
    from featured import auto_format_featured
    from ops_format import auto_format_ops_items
    from settings import resolve_query_date

    day = resolve_query_date(date)
    featured_meta = await run_in_threadpool(auto_format_featured, day, force=True)
    ops_meta = await run_in_threadpool(auto_format_ops_items, day, force=force)
    return {
        "ok": True,
        "date": day,
        "featured": {
            "source": featured_meta.get("source"),
            "total": len(featured_meta.get("items") or []),
            "skipped": featured_meta.get("skipped"),
            "skip_reason": featured_meta.get("skip_reason"),
        },
        "ops_format": {
            "processed": ops_meta.get("processed", 0),
            "ok": ops_meta.get("ok", 0),
            "failed": ops_meta.get("failed", 0),
            "skipped": ops_meta.get("skipped"),
            "skip_reason": ops_meta.get("skip_reason"),
        },
    }


@router.post("/publish-all")
def publish_all():
    """一键发布今天所有候选"""
    count = publish_all_today()
    return {"ok": True, "published": count}


@router.post("/clear-drafts")
def clear_drafts(days: int = 7):
    """清理 N 天前的旧草稿文件"""
    drafts_dir = ROOT / "data" / "drafts"
    if not drafts_dir.exists():
        return {"ok": True, "deleted": 0}
    cutoff = time.time() - days * 86400
    deleted = 0
    for f in drafts_dir.glob("*.json"):
        if f.name.endswith("_latest.json"):
            continue
        if f.stat().st_mtime < cutoff:
            f.unlink()
            deleted += 1
    return {"ok": True, "deleted": deleted}


@router.post("/cleanup-old-items")
def cleanup_items(days: int = 90):
    """清理 N 天前的 SQLite 历史记录。"""
    deleted = cleanup_old_items(days)
    return {"ok": True, "deleted": deleted, "days": max(7, int(days or 90))}
