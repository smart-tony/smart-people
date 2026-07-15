"""
行业/政策动态 — DeepSeek 内容排版（每条每天至多一次）
不处理百运百科、科技动态。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone

from db import get_today_items, list_unformatted_ops_items, update_item_llm_format
from settings import load_llm_config, today_local

_log = logging.getLogger("ops_format")
_ops_lock = threading.Lock()

OPS_TASKS = (
    "logistics-daily",
    "crossborder-platform",
    "shipping-port",
    "policy-official",
    "global-news",
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _strip_json_fence(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _body_excerpt(item: dict, max_chars: int = 1800) -> str:
    body = str(item.get("body_text") or "").strip()
    summary = str(item.get("summary") or item.get("ai_summary") or "").strip()
    text = body or summary
    try:
        from routes.scrape import strip_reading_meta

        text = strip_reading_meta(text)
    except Exception:
        pass
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for sep in ("。", "！", "？", ".", " "):
        pos = cut.rfind(sep)
        if pos > max_chars * 0.55:
            return cut[: pos + 1]
    return cut + "…"


def format_ops_item_with_llm(item: dict) -> dict:
    """排版单条行业/政策：title + summary(发生了什么) + analysis(影响)。"""
    llm_config = load_llm_config()
    api_key = str(llm_config.get("api_key") or "")
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    analysis = str(item.get("analysis") or "").strip()

    if not api_key or api_key == "***":
        return {
            "title": title,
            "summary": summary,
            "analysis": analysis,
            "llm_ok": False,
            "llm_error": "LLM 未配置",
        }

    try:
        from routes.llm import get_llm_client
    except Exception as exc:
        return {
            "title": title,
            "summary": summary,
            "analysis": analysis,
            "llm_ok": False,
            "llm_error": str(exc),
        }

    client = get_llm_client()
    if client is None:
        return {
            "title": title,
            "summary": summary,
            "analysis": analysis,
            "llm_ok": False,
            "llm_error": "LLM 客户端不可用",
        }

    source_name = str(item.get("source_name") or "").strip()
    source_url = str(item.get("source_url") or item.get("url") or "").strip()
    body_excerpt = _body_excerpt(item)

    system_prompt = (
        "你是百运网晨间星闻编辑，面向跨境物流销售与客服。"
        "只基于原文提炼核心事实，不编造、不营销话术。"
        "文案要整洁、短句、可扫读。输出严格 JSON，不要 markdown。"
    )
    user_prompt = (
        "请把下面情报整理成晨间卡片，输出 JSON：\n"
        "{\n"
        '  "title": "18-32字，有主语，去掉感叹号堆砌与频道前缀噪音",\n'
        '  "what_happened": "1-2句核心事实：谁/哪条航线或政策/发生了什么变化",\n'
        '  "impact": "1-2句业务影响：出货、清关、运价、舱位、时效或客户沟通；不确定写「需结合原文人工确认」"\n'
        "}\n\n"
        f"来源：{source_name}\n"
        f"链接：{source_url}\n"
        f"原标题：{title}\n"
        f"正文：{body_excerpt}"
    )

    try:
        response = client.chat.completions.create(
            model=llm_config.get("model", "deepseek-chat"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=min(int(llm_config.get("max_tokens", 2048)), 700),
        )
        content = _strip_json_fence(response.choices[0].message.content or "")
        result = json.loads(content)
    except Exception as exc:
        return {
            "title": title,
            "summary": summary,
            "analysis": analysis,
            "llm_ok": False,
            "llm_error": str(exc),
        }

    if not isinstance(result, dict):
        return {
            "title": title,
            "summary": summary,
            "analysis": analysis,
            "llm_ok": False,
            "llm_error": "LLM 返回格式无效",
        }

    out_title = str(result.get("title") or title).strip() or title
    what_happened = str(result.get("what_happened") or "").strip()
    impact = str(result.get("impact") or "").strip()
    if len(_CJK_RE.findall(f"{what_happened}{impact}")) < 12:
        return {
            "title": title,
            "summary": summary,
            "analysis": analysis,
            "llm_ok": False,
            "llm_error": "LLM 输出过短",
        }

    return {
        "title": out_title[:80],
        "summary": what_happened[:420],
        "analysis": impact[:420],
        "llm_ok": True,
        "llm_error": "",
    }


def auto_format_ops_items(date: str | None = None, *, force: bool = False, limit: int = 80) -> dict:
    """对当日行业/政策未排版条目逐条 DeepSeek 排版；已排版的跳过。"""
    day = date or today_local()
    with _ops_lock:
        llm_config = load_llm_config()
        api_key = str(llm_config.get("api_key") or "")
        if not api_key or api_key in {"***", "replace_me"}:
            return {
                "date": day,
                "processed": 0,
                "ok": 0,
                "failed": 0,
                "skipped": True,
                "skip_reason": "llm_unconfigured",
            }

        if force:
            pending = get_today_items(task_types=list(OPS_TASKS), limit=limit, date=day)
        else:
            pending = list_unformatted_ops_items(date=day, limit=limit)

        if not pending:
            _log.info("行业/政策排版无可处理条目 date=%s", day)
            return {
                "date": day,
                "processed": 0,
                "ok": 0,
                "failed": 0,
                "skipped": True,
                "skip_reason": "none_pending",
            }

        ok = 0
        failed = 0
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _log.info("行业/政策排版开始 date=%s pending=%d", day, len(pending))
        for item in pending:
            url = str(item.get("source_url") or "").strip()
            if not url:
                continue
            formatted = format_ops_item_with_llm(item)
            if formatted.get("llm_ok"):
                update_item_llm_format(
                    url,
                    title=formatted["title"],
                    summary=formatted["summary"],
                    analysis=formatted["analysis"],
                    formatted_at=stamp,
                )
                ok += 1
            else:
                # 失败也打标，避免同一条在每次刷新反复烧 token
                update_item_llm_format(
                    url,
                    title=str(item.get("title") or ""),
                    summary=str(item.get("summary") or ""),
                    analysis=str(item.get("analysis") or ""),
                    formatted_at=stamp,
                )
                failed += 1
                _log.warning(
                    "行业/政策排版失败 url=%s err=%s",
                    url[:80],
                    formatted.get("llm_error"),
                )

        _log.info(
            "行业/政策排版完成 date=%s processed=%d ok=%d failed=%d",
            day,
            ok + failed,
            ok,
            failed,
        )
        return {
            "date": day,
            "processed": ok + failed,
            "ok": ok,
            "failed": failed,
            "skipped": False,
        }
