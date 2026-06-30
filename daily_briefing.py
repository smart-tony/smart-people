#!/usr/bin/env python3
"""
AI 技术周报 · 素材采集脚本
============================
从 AI HOT (aihot.virxact.com) 公开 API 拉取每日精选 AI 动态，
生成编辑人可直接使用的 Markdown 素材清单。

用法:
  python daily_briefing.py                 # 今日素材清单
  python daily_briefing.py --daily          # AI 日报格式（分版块）
  python daily_briefing.py --output ~/Desktop/周报素材.md

依赖: pip install httpx
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import httpx

# ── 常量 ──────────────────────────────────────────────────
API_BASE = "https://aihot.virxact.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36"
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"

CATEGORY_CN = {
    "ai-models": "模型发布",
    "ai-products": "产品更新",
    "industry": "行业动态",
    "paper": "论文研究",
    "tip": "技巧观点",
}


# ── API 调用 ──────────────────────────────────────────────

async def fetch_items(limit: int = 50) -> dict:
    """拉取精选条目列表（含评分、标签、摘要）"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{API_BASE}/api/public/items",
            params={"mode": "selected", "limit": limit},
            headers={"User-Agent": UA},
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_daily() -> dict:
    """拉取今日 AI 日报（分版块结构）"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{API_BASE}/api/public/daily",
            headers={"User-Agent": UA},
        )
        resp.raise_for_status()
        return resp.json()


# ── 格式化输出 ────────────────────────────────────────────

def format_items_markdown(data: dict) -> str:
    """素材清单模式：按时间线列出所有精选条目"""
    today = datetime.now().strftime("%Y-%m-%d")
    items = data.get("items", [])
    count = data.get("count", len(items))

    lines = []
    lines.append(f"# AI 热点技术周报 · 素材清单")
    lines.append(f"")
    lines.append(f"**采集日期**: {today}  |  **数据来源**: [AI HOT](https://aihot.virxact.com)  |  **共 {count} 条精选**")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")

    # 按分类分组统计
    cats = {}
    for item in items:
        cat = item.get("category", "other")
        cats.setdefault(cat, []).append(item)

    lines.append(f"## 📊 分类概览")
    lines.append(f"")
    for cat_key, cat_items in sorted(cats.items(), key=lambda x: -len(x[1])):
        cn = CATEGORY_CN.get(cat_key, cat_key)
        lines.append(f"- **{cn}**: {len(cat_items)} 条")
    lines.append(f"")

    # 逐条列出
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 📋 精选条目")
    lines.append(f"")

    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        source = item.get("source", "未知来源")
        url = item.get("url") or item.get("permalink", "")
        summary = item.get("summary", "")
        score = item.get("score")
        category = item.get("category", "")

        # 分数标签
        score_str = f"⭐ {score}" if score else ""

        lines.append(f"### {i}. {title}")
        lines.append(f"")
        lines.append(f"| 字段 | 内容 |")
        lines.append(f"|------|------|")
        lines.append(f"| 来源 | {source} |")
        if score_str:
            lines.append(f"| 评分 | {score_str} |")
        if category:
            cn = CATEGORY_CN.get(category, category)
            lines.append(f"| 分类 | {cn} |")
        if url:
            lines.append(f"| 链接 | {url} |")
        lines.append(f"")
        if summary:
            # 截断过长的摘要
            if len(summary) > 500:
                summary = summary[:500] + "..."
            lines.append(f">{summary}")
            lines.append(f"")
        lines.append(f"*分析：待编辑补充*")
        lines.append(f"")

    lines.append(f"---")
    lines.append(f"*由 daily_briefing.py 自动采集 · {today}*")
    return "\n".join(lines)


def format_daily_markdown(data: dict) -> str:
    """日报模式：按版块分组（模型/产品/行业/论文/技巧）"""
    date = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    sections = data.get("sections", [])

    lines = []
    lines.append(f"# AI HOT 日报 · {date}")
    lines.append(f"")
    lines.append(f"**数据来源**: [AI HOT](https://aihot.virxact.com)")
    lines.append(f"")

    total = 0
    for sec in sections:
        label = sec.get("label", "未分类")
        items = sec.get("items", [])
        total += len(items)

        if not items:
            continue

        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## {label}（{len(items)}条）")
        lines.append(f"")

        for i, item in enumerate(items, 1):
            title = item.get("title", "无标题")
            source = item.get("sourceName", "未知来源")
            url = item.get("sourceUrl", "")
            summary = item.get("summary", "")

            lines.append(f"### {i}. {title}")
            lines.append(f"")
            lines.append(f"- **来源**: {source}")
            if url:
                lines.append(f"- **链接**: {url}")
            lines.append(f"")
            if summary:
                if len(summary) > 500:
                    summary = summary[:500] + "..."
                lines.append(f">{summary}")
            lines.append(f"")
            lines.append(f"*分析：待编辑补充*")
            lines.append(f"")

    lines.append(f"---")
    lines.append(f"*共 {total} 条 · {date}*")
    return "\n".join(lines)


# ── 主入口 ────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="AI 技术周报 · 素材采集（数据源: AI HOT API）")
    parser.add_argument("--daily", action="store_true", help="日报格式（分版块），默认素材清单格式")
    parser.add_argument("--output", "-o", help="输出文件路径")
    parser.add_argument("--limit", type=int, default=50, help="最多拉取条数 (默认50)")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n  🤖 AI 技术周报素材采集")
    print(f"  📅 {today}")
    print(f"  📡 数据源: AI HOT API (aihot.virxact.com)")
    print()

    try:
        if args.daily:
            print("  📋 拉取日报数据...")
            data = await fetch_daily()
            md = format_daily_markdown(data)
            prefix = "daily_report"
        else:
            print(f"  📋 拉取精选条目 (limit={args.limit})...")
            data = await fetch_items(args.limit)
            md = format_items_markdown(data)
            prefix = "daily_briefing"

    except httpx.HTTPError as e:
        print(f"  ❌ API 请求失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"  ❌ 处理失败: {e}")
        sys.exit(1)

    # 保存
    out_path = args.output or str(OUTPUT_DIR / f"{prefix}_{today}.md")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"  ✅ 已保存: {out_path}")
    print(f"  📏 {len(md)} 字符")

    # 打印预览
    print(f"\n{'='*60}")
    print(md[:2000])
    if len(md) > 2000:
        print(f"\n  ... (共 {len(md)} 字符，完整内容见文件)")

if __name__ == "__main__":
    asyncio.run(main())
