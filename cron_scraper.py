#!/usr/bin/env python3
"""
晨间星闻后台采集 Worker
================================
由 cron 定时调用，串行抓取行业/政策来源，存入 data/logistics_cache.json
API 直接读这个文件，秒级响应。

用法:
  python cron_scraper.py          # 抓一次
  python cron_scraper.py --loop   # 持续循环（每15分钟）

cron 配置:
  */15 * * * * cd /opt/weekly-push-tool && python cron_scraper.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests  # 改用同步 requests，避免 asyncio 连接池问题

ROOT = Path(__file__).resolve().parent
CACHE_FILE = ROOT / "data" / "logistics_cache.json"

LOGISTICS_TASKS = [
    ("logistics-daily",      "行业动态｜物流资讯"),
    ("crossborder-platform", "行业动态｜跨境平台"),
    ("shipping-port",        "行业动态｜航运港口"),
    ("by56-wiki",            "行业动态｜百运百科"),
    ("global-news",          "政策动态｜地缘与国际风险"),
    ("policy-official",      "政策动态｜关税与官方政策"),
]

BASE_URL = "http://127.0.0.1:8000"


def scrape_one(task_type, label):
    """抓取单个来源（同步，带重试）"""
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{BASE_URL}/api/scrape/fetch",
                json={
                    "task_type": task_type, "limit": 8,
                    "force_refresh": True, "recency_days": 5,
                    "analyze_with_llm": False,
                },
                timeout=90,
            )
            if resp.status_code != 200:
                print(f"  ⚠️ {label}: HTTP {resp.status_code}")
                if attempt < 2:
                    time.sleep(3)
                    continue
                return {"label": label, "task_type": task_type, "count": 0, "items": [], "ok": False,
                        "error": f"HTTP {resp.status_code}"}

            data = resp.json()
            candidates = data.get("candidates", [])
            for c in candidates:
                c["_task"] = task_type
                c["_label"] = label

            print(f"  ✅ {label}: {len(candidates)} 条")
            return {
                "label": label, "task_type": task_type,
                "count": len(candidates), "items": candidates, "ok": True,
            }
        except requests.Timeout:
            print(f"  ⏰ {label}: 超时 (attempt {attempt+1})")
            if attempt < 2:
                time.sleep(5)
                continue
            return {"label": label, "task_type": task_type, "count": 0, "items": [], "ok": False,
                    "error": "timeout after 3 retries"}
        except Exception as e:
            print(f"  ❌ {label}: {e}")
            if attempt < 2:
                time.sleep(3)
                continue
            return {"label": label, "task_type": task_type, "count": 0, "items": [], "ok": False,
                    "error": str(e)}


def scrape_all():
    """串行抓取所有来源（避免连接池打满）"""
    results = []
    for task_type, label in LOGISTICS_TASKS:
        r = scrape_one(task_type, label)
        results.append(r)
        time.sleep(1)  # 源之间稍微间隔，避免打爆服务

    all_items = []
    for r in results:
        all_items.extend(r.get("items", []))

    # 去重（按 source_url）
    seen = set()
    deduped = []
    for item in all_items:
        url = item.get("source_url", "")
        if url and url not in seen:
            seen.add(url)
            deduped.append(item)

    cache = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(deduped),
        "sources": [{k: v for k, v in r.items() if k != "items"} for r in results],
        "items": [
            {
                "title": i.get("title", ""),
                "source_name": i.get("source_name", i.get("source_id", "")),
                "source_url": i.get("source_url", ""),
                "summary": i.get("ai_summary", ""),
                "score": i.get("ai_score", 0),
                "tags": [t for t in (i.get("ai_tags") or []) if t != "未AI分析"],
                "task": i.get("_task", ""),
                "label": i.get("_label", ""),
            }
            for i in deduped
        ],
    }

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    tmp.replace(CACHE_FILE)

    return cache


def main():
    if "--loop" in sys.argv:
        print(f"🔄 持续采集模式（每15分钟）")
        while True:
            print(f"\n{'='*50}")
            print(f"📡 {datetime.now().strftime('%H:%M:%S')} 开始采集...")
            cache = scrape_all()
            ok = sum(1 for s in cache["sources"] if s.get("ok"))
            total = len(cache["sources"])
            print(f"✅ 完成: {cache['total']} 条 | {ok}/{total} 源正常")
            print(f"💤 等待 15 分钟...")
            time.sleep(900)
    else:
        print(f"📡 开始采集晨间星闻行业/政策数据...")
        cache = scrape_all()
        ok = sum(1 for s in cache["sources"] if s.get("ok"))
        total = len(cache["sources"])
        print(f"\n✅ 完成: {cache['total']} 条 | {ok}/{total} 源正常 | → {CACHE_FILE}")


if __name__ == "__main__":
    main()
