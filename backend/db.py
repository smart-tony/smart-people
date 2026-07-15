"""
数据库层 — SQLite 替代 JSON 缓存
==================================
单文件 weeks.db，Python 自带 sqlite3，零依赖
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from settings import local_date_bounds, today_local

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "weeks.db"
try:
    DB_BUSY_TIMEOUT_MS = max(1000, int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000")))
except ValueError:
    DB_BUSY_TIMEOUT_MS = 5000

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def get_db() -> sqlite3.Connection:
    """获取数据库连接（自动建表）"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=DB_BUSY_TIMEOUT_MS / 1000,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        _ensure_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    """只在进程内初始化一次 schema，避免每次读请求都触发 DDL/PRAGMA 写锁。"""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        conn.execute("PRAGMA journal_mode=WAL")
        _init_tables(conn)
        conn.commit()
        _SCHEMA_READY = True


def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source_name TEXT DEFAULT '',
            source_url TEXT UNIQUE,
            summary TEXT DEFAULT '',
            body_text TEXT DEFAULT '',
            image TEXT DEFAULT '',
            ai_score REAL DEFAULT 5.0,
            ai_tags TEXT DEFAULT '[]',
            analysis TEXT DEFAULT '',
            task_type TEXT DEFAULT '',
            status TEXT DEFAULT 'candidate',
            scraped_at TEXT NOT NULL,
            published_at TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_status ON items(status);
        CREATE INDEX IF NOT EXISTS idx_task ON items(task_type);
        CREATE INDEX IF NOT EXISTS idx_score ON items(ai_score DESC);
        CREATE INDEX IF NOT EXISTS idx_scraped ON items(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_source ON items(source_url);
    """)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """增量迁移：安全添加新列"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    migrations = [
        ("body_text", "TEXT DEFAULT ''"),
        ("image", "TEXT DEFAULT ''"),
        ("analysis", "TEXT DEFAULT ''"),
        ("llm_formatted_at", "TEXT DEFAULT ''"),
    ]
    for col, typedef in migrations:
        if col not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {typedef}")


def insert_items(items: list[dict], auto_publish: bool = True) -> int:
    """批量插入（自动跳过重复URL）。返回实际插入数。
    auto_publish=True 时直接标记为 published（适合自动抓取场景）。
    """
    if not items:
        return 0

    conn = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    status = "published" if auto_publish else "candidate"
    inserted = 0

    try:
        for item in items:
            url = item.get("source_url") or item.get("url") or ""
            if not url:
                continue
            try:
                cursor =            conn.execute("""
                INSERT OR IGNORE INTO items 
                (title, source_name, source_url, summary, body_text, image,
                 ai_score, ai_tags, analysis, task_type, status, scraped_at, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get("title", ""),
                item.get("source_name", ""),
                url,
                item.get("summary") or item.get("ai_summary", ""),
                item.get("body_text") or item.get("content_snippet", ""),
                item.get("image", ""),
                item.get("ai_score", 5.0),
                json.dumps(item.get("ai_tags") or item.get("tags") or [], ensure_ascii=False),
                item.get("analysis") or item.get("ai_analysis", ""),
                item.get("task_type") or item.get("task", ""),
                    status,
                    now,
                    now if auto_publish else "",
                ))
                if cursor.rowcount > 0:
                    inserted += 1
            except sqlite3.IntegrityError:
                pass

        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_published(task_type: str = None, limit: int = 100, date: str = "") -> list[dict]:
    """获取已发布的条目（按分数降序）。
    date: 可选 YYYY-MM-DD，限定当天数据。
    """
    conn = get_db()
    conditions = ["status = 'published'"]
    params: list = []

    if task_type:
        conditions.append("task_type = ?")
        params.append(task_type)
    if date:
        start, end = local_date_bounds(date)
        conditions.append("scraped_at >= ? AND scraped_at < ?")
        params.extend([start, end])

    sql = f"""
        SELECT * FROM items 
        WHERE {' AND '.join(conditions)}
        ORDER BY ai_score DESC, scraped_at DESC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_today_items(task_types: list[str] = None, limit: int = 200, date: str = "") -> list[dict]:
    """获取指定日期（默认今天，按业务时区）已发布的条目（日报用）。"""
    day = date.strip() or today_local()
    start, end = local_date_bounds(day)
    conn = get_db()

    if task_types:
        placeholders = ",".join("?" * len(task_types))
        rows = conn.execute(f"""
            SELECT * FROM items
            WHERE status = 'published'
            AND scraped_at >= ? AND scraped_at < ?
            AND task_type IN ({placeholders})
            ORDER BY ai_score DESC
            LIMIT ?
        """, [start, end] + task_types + [limit]).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM items
            WHERE status = 'published'
            AND scraped_at >= ? AND scraped_at < ?
            ORDER BY ai_score DESC
            LIMIT ?
        """, (start, end, limit)).fetchall()

    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_dates_with_data(limit: int = 30) -> list[str]:
    """返回有数据的日期列表（最近 N 天）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT date(scraped_at) as d
        FROM items WHERE status = 'published'
        ORDER BY d DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [r["d"] for r in rows if r["d"]]


def count_by_task(date: str = "") -> dict[str, int]:
    """按 task_type 统计条目数"""
    conn = get_db()
    if date:
        start, end = local_date_bounds(date)
        rows = conn.execute("""
            SELECT task_type, COUNT(*) as cnt FROM items
            WHERE status='published' AND scraped_at >= ? AND scraped_at < ?
            GROUP BY task_type
        """, (start, end)).fetchall()
    else:
        rows = conn.execute("""
            SELECT task_type, COUNT(*) as cnt FROM items
            WHERE status='published'
            GROUP BY task_type
        """).fetchall()
    conn.close()
    return {r["task_type"] or "other": r["cnt"] for r in rows}


def count_by_task_between(start: str, end: str) -> dict[str, int]:
    """按 task_type 统计指定 UTC 时间范围内的已发布条目。"""
    conn = get_db()
    rows = conn.execute("""
        SELECT task_type, COUNT(*) as cnt FROM items
        WHERE status='published' AND scraped_at >= ? AND scraped_at < ?
        GROUP BY task_type
    """, (start, end)).fetchall()
    conn.close()
    return {r["task_type"] or "other": r["cnt"] for r in rows}


def cleanup_old_items(days: int = 90) -> int:
    """删除 N 天前的数据，避免 SQLite 长期无限增长。"""
    days = max(7, int(days or 90))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        cursor = conn.execute("DELETE FROM items WHERE scraped_at < ?", (cutoff,))
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        conn.commit()
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
        return deleted
    finally:
        conn.close()


def get_candidates(limit: int = 50) -> list[dict]:
    """获取候选条目（待筛选）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM items 
        WHERE status = 'candidate'
        ORDER BY scraped_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def update_status(source_url: str, status: str):
    """标记条目状态：candidate / published / hidden"""
    conn = get_db()
    conn.execute("UPDATE items SET status = ? WHERE source_url = ?", (status, source_url))
    conn.commit()
    conn.close()


OPS_FORMAT_TASKS = (
    "logistics-daily",
    "crossborder-platform",
    "shipping-port",
    "policy-official",
    "global-news",
)


def list_unformatted_ops_items(date: str = "", limit: int = 80) -> list[dict]:
    """当日行业/政策中尚未 DeepSeek 排版的条目（不含百科）。"""
    day = (date or "").strip() or today_local()
    start, end = local_date_bounds(day)
    placeholders = ",".join("?" * len(OPS_FORMAT_TASKS))
    conn = get_db()
    rows = conn.execute(
        f"""
        SELECT * FROM items
        WHERE status = 'published'
          AND scraped_at >= ? AND scraped_at < ?
          AND task_type IN ({placeholders})
          AND (llm_formatted_at IS NULL OR llm_formatted_at = '')
        ORDER BY ai_score DESC, scraped_at DESC
        LIMIT ?
        """,
        [start, end, *OPS_FORMAT_TASKS, limit],
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def update_item_llm_format(
    source_url: str,
    *,
    title: str,
    summary: str,
    analysis: str,
    formatted_at: str = "",
) -> None:
    """写入 DeepSeek 排版结果，并打标避免重复消耗 token。"""
    stamp = formatted_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE items
            SET title = ?, summary = ?, analysis = ?, llm_formatted_at = ?
            WHERE source_url = ?
            """,
            (title, summary, analysis, stamp, source_url),
        )
        conn.commit()
    finally:
        conn.close()


def publish_all_today():
    """一键发布今天所有候选（按业务时区「今天」）"""
    conn = get_db()
    start, end = local_date_bounds(today_local())
    conn.execute("""
        UPDATE items SET status = 'published' 
        WHERE status = 'candidate' AND scraped_at >= ? AND scraped_at < ?
    """, (start, end))
    count = conn.total_changes
    conn.commit()
    conn.close()
    return count


def get_stats() -> dict:
    """统计概览"""
    conn = get_db()
    start, end = local_date_bounds(today_local())

    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    published = conn.execute("SELECT COUNT(*) FROM items WHERE status='published'").fetchone()[0]
    candidates = conn.execute("SELECT COUNT(*) FROM items WHERE status='candidate'").fetchone()[0]
    today_count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE scraped_at >= ? AND scraped_at < ?",
        (start, end),
    ).fetchone()[0]

    tasks = conn.execute("""
        SELECT task_type, COUNT(*) as cnt 
        FROM items WHERE status='published'
        GROUP BY task_type ORDER BY cnt DESC
    """).fetchall()

    conn.close()
    return {
        "total": total,
        "published": published,
        "candidates": candidates,
        "today": today_count,
        "by_task": {r["task_type"] or "other": r["cnt"] for r in tasks},
    }


def _row_to_dict(row) -> dict:
    d = dict(row)
    # 解析 JSON 字段
    try:
        d["ai_tags"] = json.loads(d.get("ai_tags", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["ai_tags"] = []
    return d
