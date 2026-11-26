"""SQLite persistence for accounts, post history, and local scheduled queue."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from config import APP_DIR

DB_PATH = APP_DIR / "data.db"


@contextmanager
def _conn():
    """Open a SQLite connection, commit/rollback transaction, and always close it."""
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def init() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS accounts(
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                page_id TEXT NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(platform, page_id)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS post_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                platform TEXT NOT NULL,
                page_id TEXT NOT NULL,
                page_name TEXT NOT NULL,
                message TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                image_paths TEXT NOT NULL DEFAULT '[]',
                video_path TEXT NOT NULL DEFAULT '',
                scheduled_for TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                remote_post_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT ''
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS scheduled_queue(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                run_at TEXT NOT NULL,
                message TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                image_paths TEXT NOT NULL DEFAULT '[]',
                video_path TEXT NOT NULL DEFAULT '',
                targets_json TEXT NOT NULL,
                comments_json TEXT NOT NULL DEFAULT '[]',
                comment_delay REAL NOT NULL DEFAULT 2.0,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT ''
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS templates(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'caption',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS saved_groups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS page_presets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                page_keys_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS saved_places(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'place',
                name TEXT NOT NULL,
                fb_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(kind, fb_id)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS page_analytics_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                page_id TEXT NOT NULL,
                page_name TEXT NOT NULL,
                period_days INTEGER NOT NULL DEFAULT 7,
                reach INTEGER NOT NULL DEFAULT 0,
                impressions INTEGER NOT NULL DEFAULT 0,
                engagement INTEGER NOT NULL DEFAULT 0,
                reactions INTEGER NOT NULL DEFAULT 0,
                comments INTEGER NOT NULL DEFAULT 0,
                shares INTEGER NOT NULL DEFAULT 0,
                link_clicks INTEGER NOT NULL DEFAULT 0,
                followers INTEGER NOT NULL DEFAULT 0,
                prev_reach INTEGER NOT NULL DEFAULT 0,
                prev_engagement INTEGER NOT NULL DEFAULT 0,
                reach_delta_pct REAL,
                engagement_delta_pct REAL,
                latest_post_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ok',
                error TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE INDEX IF NOT EXISTS idx_analytics_page_period
               ON page_analytics_snapshots(platform, page_id, period_days, id DESC)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS post_presets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                message TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '',
                image_paths TEXT NOT NULL DEFAULT '[]',
                video_path TEXT NOT NULL DEFAULT '',
                is_reel INTEGER NOT NULL DEFAULT 0,
                watermark_enabled INTEGER NOT NULL DEFAULT 0,
                watermark_text TEXT NOT NULL DEFAULT '',
                place_id TEXT NOT NULL DEFAULT '',
                place_name TEXT NOT NULL DEFAULT '',
                comments_json TEXT NOT NULL DEFAULT '[]',
                comment_delay REAL NOT NULL DEFAULT 2.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS auto_reply_rules(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                match_mode TEXT NOT NULL DEFAULT 'contains',
                reply_template TEXT NOT NULL,
                hide_after_reply INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS auto_reply_log(
                comment_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                page_id TEXT NOT NULL,
                post_id TEXT NOT NULL,
                rule_id INTEGER NOT NULL DEFAULT 0,
                reply_comment_id TEXT NOT NULL DEFAULT '',
                commenter_name TEXT NOT NULL DEFAULT '',
                comment_text TEXT NOT NULL DEFAULT '',
                reply_text TEXT NOT NULL DEFAULT '',
                replied_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE INDEX IF NOT EXISTS idx_auto_reply_log_time
               ON auto_reply_log(replied_at DESC)"""
        )
        # Safe migrations for existing databases
        ph_cols = {r["name"] for r in c.execute("PRAGMA table_info(post_history)").fetchall()}
        for col_name in (
            "video_path",
            "batch_id",
            "place_id",
            "place_name",
            "og_action_type_id",
            "og_object_id",
            "og_label",
        ):
            if col_name not in ph_cols:
                c.execute(f"ALTER TABLE post_history ADD COLUMN {col_name} TEXT NOT NULL DEFAULT ''")
        if "is_reel" not in ph_cols:
            c.execute("ALTER TABLE post_history ADD COLUMN is_reel INTEGER NOT NULL DEFAULT 0")

        sq_cols = {r["name"] for r in c.execute("PRAGMA table_info(scheduled_queue)").fetchall()}
        if "comments_json" not in sq_cols:
            c.execute("ALTER TABLE scheduled_queue ADD COLUMN comments_json TEXT NOT NULL DEFAULT '[]'")
        if "comment_delay" not in sq_cols:
            c.execute("ALTER TABLE scheduled_queue ADD COLUMN comment_delay REAL NOT NULL DEFAULT 2.0")
        if "video_path" not in sq_cols:
            c.execute("ALTER TABLE scheduled_queue ADD COLUMN video_path TEXT NOT NULL DEFAULT ''")
        if "repeat_rule" not in sq_cols:
            c.execute("ALTER TABLE scheduled_queue ADD COLUMN repeat_rule TEXT NOT NULL DEFAULT 'none'")
        if "watermark_text" not in sq_cols:
            c.execute("ALTER TABLE scheduled_queue ADD COLUMN watermark_text TEXT NOT NULL DEFAULT ''")
        for col_name in (
            "place_id",
            "place_name",
            "og_action_type_id",
            "og_object_id",
            "og_label",
        ):
            if col_name not in sq_cols:
                c.execute(
                    f"ALTER TABLE scheduled_queue ADD COLUMN {col_name} TEXT NOT NULL DEFAULT ''"
                )
        if "is_reel" not in sq_cols:
            c.execute("ALTER TABLE scheduled_queue ADD COLUMN is_reel INTEGER NOT NULL DEFAULT 0")


# --- Accounts ---

def save_account(platform: str, page_id: str, name: str) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO accounts(platform, page_id, name) VALUES(?, ?, ?)
               ON CONFLICT(platform, page_id) DO UPDATE SET name=excluded.name""",
            (platform, page_id, name),
        )


def list_accounts() -> list[tuple[str, str, str]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT platform, page_id, name FROM accounts ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [(r["platform"], r["page_id"], r["name"]) for r in rows]


def delete_account(platform: str, page_id: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM accounts WHERE platform=? AND page_id=?",
            (platform, page_id),
        )
        c.execute(
            "DELETE FROM page_analytics_snapshots WHERE platform=? AND page_id=?",
            (platform, page_id),
        )


# --- Post History ---

def _decode_history_row(r: sqlite3.Row) -> dict:
    item = dict(r)
    try:
        item["image_paths"] = json.loads(item["image_paths"] or "[]")
    except ValueError:
        item["image_paths"] = []
    item["is_reel"] = bool(item.get("is_reel", 0))
    return item


def add_post_history(
    *,
    platform: str,
    page_id: str,
    page_name: str,
    message: str,
    link: str = "",
    image_paths: list[str] | None = None,
    video_path: str = "",
    is_reel: bool = False,
    scheduled_for: str = "",
    status: str = "published",
    remote_post_id: str = "",
    error: str = "",
    batch_id: str = "",
    place_id: str = "",
    place_name: str = "",
    og_action_type_id: str = "",
    og_object_id: str = "",
    og_label: str = "",
) -> int:
    images_json = json.dumps(list(image_paths or []), ensure_ascii=False)
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO post_history(
                   created_at, platform, page_id, page_name, message, link,
                   image_paths, video_path, is_reel, scheduled_for, status, remote_post_id, error, batch_id,
                   place_id, place_name, og_action_type_id, og_object_id, og_label
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(),
                platform,
                page_id,
                page_name,
                message,
                link or "",
                images_json,
                video_path or "",
                1 if (is_reel and video_path) else 0,
                scheduled_for or "",
                status,
                remote_post_id or "",
                error or "",
                batch_id or "",
                place_id or "",
                place_name or "",
                og_action_type_id or "",
                og_object_id or "",
                og_label or "",
            ),
        )
        return int(cur.lastrowid)


def list_post_history(status_filter: str = "all", limit: int = 200) -> list[dict]:
    with _conn() as c:
        if status_filter and status_filter != "all":
            rows = c.execute(
                """SELECT * FROM post_history
                   WHERE status=?
                   ORDER BY id DESC LIMIT ?""",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM post_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_decode_history_row(r) for r in rows]


def get_post_history(entry_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM post_history WHERE id=?", (entry_id,)).fetchone()
    if not r:
        return None
    return _decode_history_row(r)


def delete_post_history(entry_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM post_history WHERE id=?", (entry_id,))


def update_post_history_message(entry_id: int, new_message: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE post_history SET message=? WHERE id=?",
            (new_message, entry_id),
        )


def update_post_history_message_by_remote_id(
    platform: str,
    page_id: str,
    remote_post_id: str,
    new_message: str,
) -> int:
    if not remote_post_id:
        return 0
    with _conn() as c:
        cur = c.execute(
            """UPDATE post_history
               SET message=?
               WHERE platform=? AND page_id=? AND remote_post_id=?""",
            (new_message, platform, page_id, remote_post_id),
        )
        return cur.rowcount


def get_post_history_by_remote_id(
    platform: str,
    page_id: str,
    remote_post_id: str,
) -> dict | None:
    if not remote_post_id:
        return None
    with _conn() as c:
        r = c.execute(
            """SELECT * FROM post_history
               WHERE platform=? AND page_id=? AND remote_post_id=?
               ORDER BY id DESC LIMIT 1""",
            (platform, page_id, remote_post_id),
        ).fetchone()
    if not r:
        return None
    return _decode_history_row(r)


def update_post_history_entry_by_remote_id(
    platform: str,
    page_id: str,
    old_remote_post_id: str,
    *,
    message: str,
    link: str = "",
    image_paths: list[str] | None = None,
    video_path: str = "",
    new_remote_post_id: str | None = None,
    status: str | None = None,
    place_id: str | None = None,
    place_name: str | None = None,
    og_action_type_id: str | None = None,
    og_object_id: str | None = None,
    og_label: str | None = None,
) -> int:
    if not old_remote_post_id:
        return 0
    images_json = json.dumps(list(image_paths or []), ensure_ascii=False)
    target_remote_id = new_remote_post_id if new_remote_post_id is not None else old_remote_post_id
    eff_status = status or "published"
    fields = [
        "message=?",
        "link=?",
        "image_paths=?",
        "video_path=?",
        "remote_post_id=?",
        "status=?",
        "error=''",
    ]
    params: list[object] = [
        message,
        link or "",
        images_json,
        video_path or "",
        target_remote_id,
        eff_status,
    ]
    for col, val in (
        ("place_id", place_id),
        ("place_name", place_name),
        ("og_action_type_id", og_action_type_id),
        ("og_object_id", og_object_id),
        ("og_label", og_label),
    ):
        if val is not None:
            fields.append(f"{col}=?")
            params.append(val)
    params.extend([platform, page_id, old_remote_post_id])
    with _conn() as c:
        cur = c.execute(
            f"""UPDATE post_history
                SET {', '.join(fields)}
                WHERE platform=? AND page_id=? AND remote_post_id=?""",
            tuple(params),
        )
        return cur.rowcount


def update_post_history_entry(
    entry_id: int,
    *,
    message: str,
    link: str = "",
    image_paths: list[str] | None = None,
    video_path: str = "",
    remote_post_id: str | None = None,
    status: str | None = None,
    place_id: str | None = None,
    place_name: str | None = None,
    og_action_type_id: str | None = None,
    og_object_id: str | None = None,
    og_label: str | None = None,
) -> None:
    images_json = json.dumps(list(image_paths or []), ensure_ascii=False)
    eff_status = status or "published"
    fields = ["message=?", "link=?", "image_paths=?", "video_path=?"]
    params: list[object] = [message, link or "", images_json, video_path or ""]
    if remote_post_id is not None:
        fields.extend(["remote_post_id=?", "status=?", "error=''"])
        params.extend([remote_post_id, eff_status])
    for col, val in (
        ("place_id", place_id),
        ("place_name", place_name),
        ("og_action_type_id", og_action_type_id),
        ("og_object_id", og_object_id),
        ("og_label", og_label),
    ):
        if val is not None:
            fields.append(f"{col}=?")
            params.append(val)
    params.append(entry_id)
    with _conn() as c:
        c.execute(
            f"UPDATE post_history SET {', '.join(fields)} WHERE id=?",
            tuple(params),
        )


def list_post_history_by_batch(batch_id: str) -> list[dict]:
    if not batch_id:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM post_history WHERE batch_id=? ORDER BY id ASC",
            (batch_id,),
        ).fetchall()
    return [_decode_history_row(r) for r in rows]


def delete_post_history_by_remote_id(
    platform: str,
    page_id: str,
    remote_post_id: str,
) -> int:
    if not remote_post_id:
        return 0
    with _conn() as c:
        cur = c.execute(
            """DELETE FROM post_history
               WHERE platform=? AND page_id=? AND remote_post_id=?""",
            (platform, page_id, remote_post_id),
        )
        return cur.rowcount


def clear_post_history() -> None:
    with _conn() as c:
        c.execute("DELETE FROM post_history")


def count_post_history(status_filter: str = "published") -> int:
    with _conn() as c:
        if status_filter and status_filter != "all":
            row = c.execute(
                "SELECT COUNT(*) AS cnt FROM post_history WHERE status=?",
                (status_filter,),
            ).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) AS cnt FROM post_history").fetchone()
        return int(row["cnt"]) if row else 0


# --- Local Scheduled Queue ---

def add_scheduled_job(
    *,
    run_at_iso: str,
    message: str,
    link: str = "",
    image_paths: list[str] | None = None,
    video_path: str = "",
    is_reel: bool = False,
    targets: list[dict],
    comments: list[str] | None = None,
    comment_delay: float = 2.0,
    repeat_rule: str = "none",
    watermark_text: str = "",
    place_id: str = "",
    place_name: str = "",
    og_action_type_id: str = "",
    og_object_id: str = "",
    og_label: str = "",
) -> int:
    rule = repeat_rule if repeat_rule in ("none", "daily", "weekly") else "none"
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO scheduled_queue(
                   created_at, run_at, message, link, image_paths, video_path, is_reel, targets_json,
                   comments_json, comment_delay, repeat_rule, watermark_text,
                   place_id, place_name, og_action_type_id, og_object_id, og_label, status
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                _now_iso(),
                run_at_iso,
                message,
                link or "",
                json.dumps(list(image_paths or []), ensure_ascii=False),
                video_path or "",
                1 if (is_reel and video_path) else 0,
                json.dumps(targets, ensure_ascii=False),
                json.dumps(list(comments or []), ensure_ascii=False),
                float(comment_delay),
                rule,
                (watermark_text or "").strip(),
                place_id or "",
                place_name or "",
                og_action_type_id or "",
                og_object_id or "",
                og_label or "",
            ),
        )
        return int(cur.lastrowid)


def _decode_job(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["image_paths"] = json.loads(item["image_paths"] or "[]")
    except ValueError:
        item["image_paths"] = []
    try:
        item["targets"] = json.loads(item["targets_json"] or "[]")
    except ValueError:
        item["targets"] = []
    try:
        item["comments"] = json.loads(item.get("comments_json") or "[]")
    except ValueError:
        item["comments"] = []
    item["comment_delay"] = (
        2.0 if item.get("comment_delay") is None else float(item["comment_delay"])
    )
    item["repeat_rule"] = item.get("repeat_rule") or "none"
    item["watermark_text"] = item.get("watermark_text") or ""
    item["place_id"] = item.get("place_id") or ""
    item["place_name"] = item.get("place_name") or ""
    item["og_action_type_id"] = item.get("og_action_type_id") or ""
    item["og_object_id"] = item.get("og_object_id") or ""
    item["og_label"] = item.get("og_label") or ""
    item["is_reel"] = bool(item.get("is_reel", 0))
    return item


def list_scheduled_jobs(include_done: bool = False) -> list[dict]:
    with _conn() as c:
        if include_done:
            rows = c.execute(
                "SELECT * FROM scheduled_queue ORDER BY run_at ASC, id ASC"
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT * FROM scheduled_queue
                   WHERE status IN ('pending', 'running', 'failed')
                   ORDER BY run_at ASC, id ASC"""
            ).fetchall()
    return [_decode_job(r) for r in rows]


def get_due_scheduled_jobs(now_dt: datetime | None = None) -> list[dict]:
    now = now_dt or datetime.now(timezone.utc).astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    due = []
    for job in list_scheduled_jobs(include_done=False):
        if job["status"] != "pending":
            continue
        try:
            run_dt = datetime.fromisoformat(job["run_at"])
            if run_dt.tzinfo is None:
                run_dt = run_dt.astimezone()
            if run_dt <= now:
                due.append(job)
        except (ValueError, OSError, OverflowError):
            continue
    return due


def get_scheduled_job(job_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM scheduled_queue WHERE id=?", (job_id,)).fetchone()
    return _decode_job(r) if r else None


def update_scheduled_job_status(job_id: int, status: str, last_error: str = "") -> None:
    with _conn() as c:
        c.execute(
            "UPDATE scheduled_queue SET status=?, last_error=? WHERE id=?",
            (status, last_error or "", job_id),
        )


def update_scheduled_job(
    job_id: int,
    *,
    run_at_iso: str,
    message: str,
    link: str = "",
    repeat_rule: str = "none",
    watermark_text: str | None = None,
    image_paths: list[str] | None = None,
    video_path: str | None = None,
    is_reel: bool | None = None,
    place_id: str | None = None,
    place_name: str | None = None,
    og_action_type_id: str | None = None,
    og_object_id: str | None = None,
    og_label: str | None = None,
) -> None:
    rule = repeat_rule if repeat_rule in ("none", "daily", "weekly") else "none"
    fields = ["run_at=?", "message=?", "link=?", "repeat_rule=?", "status='pending'", "last_error=''"]
    params: list[object] = [run_at_iso, message, link or "", rule]
    if watermark_text is not None:
        fields.append("watermark_text=?")
        params.append(watermark_text.strip())
    if image_paths is not None:
        fields.append("image_paths=?")
        params.append(json.dumps(list(image_paths), ensure_ascii=False))
    if video_path is not None:
        fields.append("video_path=?")
        params.append(video_path or "")
    if is_reel is not None:
        fields.append("is_reel=?")
        params.append(1 if is_reel else 0)
    for col, val in (
        ("place_id", place_id),
        ("place_name", place_name),
        ("og_action_type_id", og_action_type_id),
        ("og_object_id", og_object_id),
        ("og_label", og_label),
    ):
        if val is not None:
            fields.append(f"{col}=?")
            params.append(val)
    params.append(job_id)
    with _conn() as c:
        c.execute(
            f"UPDATE scheduled_queue SET {', '.join(fields)} WHERE id=?",
            tuple(params),
        )


def retry_failed_scheduled_jobs() -> int:
    with _conn() as c:
        cur = c.execute(
            """UPDATE scheduled_queue
               SET status='pending', last_error=''
               WHERE status='failed'"""
        )
        return cur.rowcount


def advance_recurring_scheduled_job(
    job_id: int,
    now_dt: datetime | None = None,
) -> str | None:
    """If job has repeat_rule in ('daily', 'weekly'), advance run_at to next future cycle and reset to 'pending'."""
    job = get_scheduled_job(job_id)
    if not job:
        return None
    rule = job.get("repeat_rule") or "none"
    if rule not in ("daily", "weekly"):
        return None
    step = timedelta(days=1 if rule == "daily" else 7)
    now = now_dt or datetime.now(timezone.utc).astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    try:
        next_dt = datetime.fromisoformat(job["run_at"])
        if next_dt.tzinfo is None:
            next_dt = next_dt.astimezone()
    except (ValueError, OSError, OverflowError):
        next_dt = now
    next_dt += step
    while next_dt <= now:
        next_dt += step
    next_iso = next_dt.isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            "UPDATE scheduled_queue SET run_at=?, status='pending', last_error='' WHERE id=?",
            (next_iso, job_id),
        )
    return next_iso


def delete_scheduled_job(job_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM scheduled_queue WHERE id=?", (job_id,))


def recover_stuck_scheduled_jobs() -> int:
    """Reset any jobs left in 'running' state (e.g. after a crash) back to 'failed'."""
    with _conn() as c:
        cur = c.execute(
            """UPDATE scheduled_queue
               SET status='failed', last_error='โปรแกรมถูกปิดระหว่างกำลังโพสต์ (กรุณาตรวจสอบหรือกดโพสต์ใหม่)'
               WHERE status='running'"""
        )
        return cur.rowcount


# --- Caption Templates & Hashtag Sets ---

def add_template(name: str, kind: str, content: str) -> int:
    kind = kind if kind in ("caption", "hashtag") else "caption"
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO templates(name, kind, content, created_at)
               VALUES(?, ?, ?, ?)""",
            (name.strip(), kind, content.strip(), _now_iso()),
        )
        return int(cur.lastrowid)


def list_templates(kind: str = "all") -> list[dict]:
    with _conn() as c:
        if kind in ("caption", "hashtag"):
            rows = c.execute(
                "SELECT * FROM templates WHERE kind=? ORDER BY name COLLATE NOCASE, id DESC",
                (kind,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM templates ORDER BY kind ASC, name COLLATE NOCASE, id DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def delete_template(template_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM templates WHERE id=?", (template_id,))


# --- Saved Facebook Groups (Quick Share Helper) ---

def add_saved_group(name: str, url: str, note: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO saved_groups(name, url, note, created_at)
               VALUES(?, ?, ?, ?)""",
            (name.strip(), url.strip(), (note or "").strip(), _now_iso()),
        )
        return int(cur.lastrowid)


def list_saved_groups() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM saved_groups ORDER BY name COLLATE NOCASE, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_saved_group(group_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM saved_groups WHERE id=?", (group_id,))


# --- Page Presets (Saved Page Sets) ---

def save_page_preset(name: str, page_keys: list[tuple[str, str] | str]) -> int:
    clean_name = name.strip()
    norm_keys: list[list[str]] = []
    for k in page_keys:
        if isinstance(k, (tuple, list)) and len(k) == 2:
            norm_keys.append([str(k[0]), str(k[1])])
        elif isinstance(k, str) and ":" in k:
            pl, pid = k.split(":", 1)
            norm_keys.append([pl, pid])
        elif isinstance(k, str) and k:
            norm_keys.append(["facebook", k])
    payload = json.dumps(norm_keys, ensure_ascii=False)
    with _conn() as c:
        c.execute(
            """INSERT INTO page_presets(name, page_keys_json, created_at)
               VALUES(?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET page_keys_json=excluded.page_keys_json""",
            (clean_name, payload, _now_iso()),
        )
        row = c.execute("SELECT id FROM page_presets WHERE name=?", (clean_name,)).fetchone()
        return int(row["id"]) if row else 0


def list_page_presets() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM page_presets ORDER BY name COLLATE NOCASE, id DESC"
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            raw_keys = json.loads(item["page_keys_json"] or "[]")
            item["page_keys"] = [(str(k[0]), str(k[1])) for k in raw_keys if isinstance(k, list) and len(k) == 2]
        except ValueError:
            item["page_keys"] = []
        out.append(item)
    return out


def delete_page_preset(preset_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM page_presets WHERE id=?", (preset_id,))


# --- Saved Check-in Places & Activity Target Pages ---

def save_place_preset(kind: str, name: str, fb_id: str) -> int:
    clean_kind = kind if kind in ("place", "activity_target") else "place"
    clean_name = (name or "").strip() or (fb_id or "").strip()
    clean_id = (fb_id or "").strip()
    if not clean_id:
        raise ValueError("กรุณาระบุเลข Facebook ID ก่อนบันทึก")
    with _conn() as c:
        c.execute(
            """INSERT INTO saved_places(kind, name, fb_id, created_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(kind, fb_id) DO UPDATE SET name=excluded.name""",
            (clean_kind, clean_name, clean_id, _now_iso()),
        )
        row = c.execute(
            "SELECT id FROM saved_places WHERE kind=? AND fb_id=?",
            (clean_kind, clean_id),
        ).fetchone()
        return int(row["id"]) if row else 0


def list_place_presets(kind: str | None = None) -> list[dict]:
    with _conn() as c:
        if kind in ("place", "activity_target"):
            rows = c.execute(
                "SELECT * FROM saved_places WHERE kind=? ORDER BY name COLLATE NOCASE, id DESC",
                (kind,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM saved_places ORDER BY kind ASC, name COLLATE NOCASE, id DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def delete_place_preset(preset_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM saved_places WHERE id=?", (preset_id,))


# --- Page Analytics Snapshots ---

def save_page_analytics_snapshot(
    *,
    platform: str,
    page_id: str,
    page_name: str,
    period_days: int = 7,
    reach: int = 0,
    impressions: int = 0,
    engagement: int = 0,
    reactions: int = 0,
    comments: int = 0,
    shares: int = 0,
    link_clicks: int = 0,
    followers: int = 0,
    prev_reach: int = 0,
    prev_engagement: int = 0,
    reach_delta_pct: float | None = None,
    engagement_delta_pct: float | None = None,
    latest_post_at: str = "",
    status: str = "ok",
    error: str = "",
    fetched_at: str = "",
) -> int:
    now_iso = (fetched_at or "").strip() or datetime.now().astimezone().isoformat(timespec="seconds")
    p_days = max(1, min(int(period_days or 7), 90))
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO page_analytics_snapshots(
                   platform, page_id, page_name, period_days,
                   reach, impressions, engagement, reactions, comments, shares, link_clicks, followers,
                   prev_reach, prev_engagement, reach_delta_pct, engagement_delta_pct,
                   latest_post_at, status, error, fetched_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                platform,
                page_id,
                page_name or page_id,
                p_days,
                int(reach or 0),
                int(impressions or 0),
                int(engagement or 0),
                int(reactions or 0),
                int(comments or 0),
                int(shares or 0),
                int(link_clicks or 0),
                int(followers or 0),
                int(prev_reach or 0),
                int(prev_engagement or 0),
                None if reach_delta_pct is None else float(reach_delta_pct),
                None if engagement_delta_pct is None else float(engagement_delta_pct),
                latest_post_at or "",
                status or "ok",
                error or "",
                now_iso,
            ),
        )
        # Keep only the latest 30 snapshots per (platform, page_id, period_days)
        c.execute(
            """DELETE FROM page_analytics_snapshots
               WHERE platform=? AND page_id=? AND period_days=?
                 AND id NOT IN (
                     SELECT id FROM page_analytics_snapshots
                     WHERE platform=? AND page_id=? AND period_days=?
                     ORDER BY id DESC LIMIT 30
                 )""",
            (platform, page_id, p_days, platform, page_id, p_days),
        )
        return int(cur.lastrowid)


def get_latest_page_analytics(period_days: int = 7) -> list[dict]:
    p_days = max(1, min(int(period_days or 7), 90))
    with _conn() as c:
        rows = c.execute(
            """SELECT s.*
               FROM page_analytics_snapshots s
               INNER JOIN accounts a
                   ON s.platform = a.platform AND s.page_id = a.page_id
               INNER JOIN (
                   SELECT platform, page_id, MAX(id) AS max_id
                   FROM page_analytics_snapshots
                   WHERE period_days=?
                   GROUP BY platform, page_id
               ) latest ON s.id = latest.max_id
               ORDER BY s.reach DESC, s.engagement DESC, s.page_name COLLATE NOCASE""",
            (p_days,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_previous_page_analytics(
    platform: str, page_id: str, period_days: int = 7
) -> dict | None:
    p_days = max(1, min(int(period_days or 7), 90))
    with _conn() as c:
        row = c.execute(
            """SELECT * FROM page_analytics_snapshots
               WHERE platform=? AND page_id=? AND period_days=? AND status != 'error'
               ORDER BY id DESC LIMIT 1""",
            (platform, page_id, p_days),
        ).fetchone()
    return dict(row) if row else None


def backup_database(dest_path: str | Path) -> None:
    """Create a consistent online SQLite backup (WAL-safe) at `dest_path`."""
    dest = Path(dest_path).resolve()
    if dest == DB_PATH.resolve():
        raise ValueError("ไม่สามารถสำรองทับไฟล์ฐานข้อมูลต้นฉบับที่กำลังใช้งานอยู่ได้")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as src:
        dst = sqlite3.connect(dest, timeout=15.0)
        try:
            src.backup(dst)
        finally:
            dst.close()


def get_backup_dir() -> Path:
    return DB_PATH.parent / "backups"


def run_daily_auto_backup(
    keep_days: int = 7,
    *,
    today_str: str | None = None,
) -> Path | None:
    """Create a WAL-safe daily rolling backup (`auto_backup_YYYY-MM-DD.db`) and keep newest `keep_days`."""
    if not DB_PATH.is_file() or DB_PATH.stat().st_size == 0:
        return None
    keep = max(1, min(int(keep_days or 7), 365))
    day_label = (
        today_str or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    ).strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day_label, flags=re.ASCII):
        raise ValueError(f"รูปแบบวันที่สำรองข้อมูลไม่ถูกต้อง: {day_label}")

    bdir = get_backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    target = bdir / f"auto_backup_{day_label}.db"
    if not target.is_file():
        tmp = bdir / f".auto_backup_{day_label}.tmp_{os.getpid()}_{uuid.uuid4().hex}.db"
        try:
            backup_database(tmp)
            os.replace(tmp, target)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    auto_files = sorted(
        (
            p
            for p in bdir.iterdir()
            if p.is_file()
            and re.fullmatch(
                r"auto_backup_[0-9]{4}-[0-9]{2}-[0-9]{2}\.db",
                p.name,
                flags=re.ASCII,
            )
        ),
        key=lambda p: p.name,
        reverse=True,
    )
    for stale in auto_files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def validate_sqlite_backup_file(src_path: str | Path) -> Path:
    """Verify `src_path` is a valid, uncorrupted Social Manager SQLite backup file."""
    raw = str(src_path or "").strip()
    if not raw:
        raise ValueError("กรุณาเลือกไฟล์ฐานข้อมูลสำรอง (.db)")
    src = Path(raw).resolve()
    if not src.is_file():
        raise ValueError(f"ไม่พบไฟล์ฐานข้อมูลสำรอง: {src}")
    if src == DB_PATH.resolve():
        raise ValueError("ไม่สามารถกู้คืนทับจากไฟล์ฐานข้อมูลที่กำลังใช้งานอยู่โดยตรงได้")

    try:
        with src.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        raise ValueError(f"ไม่สามารถอ่านไฟล์สำรองได้: {exc}") from exc
    if header != b"SQLite format 3\x00":
        raise ValueError("ไฟล์ที่เลือกไม่ใช่ฐานข้อมูล SQLite ที่ถูกต้อง")

    conn = sqlite3.connect(src, timeout=10.0)
    try:
        try:
            qc_rows = conn.execute("PRAGMA quick_check").fetchall()
            if (
                not qc_rows
                or len(qc_rows) != 1
                or str(qc_rows[0][0]).strip().lower() != "ok"
            ):
                raise ValueError("ไฟล์ฐานข้อมูลสำรองไม่สมบูรณ์ (PRAGMA quick_check ไม่ผ่าน)")
            tbl_row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone()
            if not tbl_row:
                raise ValueError(
                    "ไฟล์ SQLite นี้ไม่ใช่ฐานข้อมูลของโปรแกรม โพสต์ดิ!! รอไร (ไม่พบตาราง accounts)"
                )
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"ไฟล์ฐานข้อมูลสำรองเสียหาย: {exc}") from exc
    finally:
        conn.close()
    return src


def restore_database(src_path: str | Path) -> Path | None:
    """Validate `src_path`, save a `pre_restore_*.db` safety backup, restore via online `.backup()`, and run `init()`."""
    src = validate_sqlite_backup_file(src_path)
    pre_backup: Path | None = None
    if DB_PATH.is_file() and DB_PATH.stat().st_size > 0:
        bdir = get_backup_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
        pre_backup = bdir / f"pre_restore_{stamp}_{uuid.uuid4().hex[:6]}.db"
        backup_database(pre_backup)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(src, timeout=15.0)
    dst_conn = sqlite3.connect(DB_PATH, timeout=15.0)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    init()
    return pre_backup


# --- Post Presets (Content Packs) ---

def _decode_post_preset(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["image_paths"] = json.loads(item.get("image_paths") or "[]")
    except ValueError:
        item["image_paths"] = []
    try:
        item["comments"] = json.loads(item.get("comments_json") or "[]")
    except ValueError:
        item["comments"] = []
    item["is_reel"] = bool(item.get("is_reel", 0))
    item["watermark_enabled"] = bool(item.get("watermark_enabled", 0))
    item["comment_delay"] = (
        2.0 if item.get("comment_delay") is None else float(item["comment_delay"])
    )
    return item


def save_post_preset(
    *,
    name: str,
    message: str = "",
    link: str = "",
    image_paths: list[str] | None = None,
    video_path: str = "",
    is_reel: bool = False,
    watermark_enabled: bool = False,
    watermark_text: str = "",
    place_id: str = "",
    place_name: str = "",
    comments: list[str] | None = None,
    comment_delay: float = 2.0,
) -> int:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("กรุณาตั้งชื่อชุดโพสต์สำเร็จรูป")
    images = [str(p).strip() for p in (image_paths or []) if str(p).strip()]
    clean_video = (video_path or "").strip()
    if images and clean_video:
        raise ValueError("ชุดโพสต์ไม่สามารถแนบทั้งรูปภาพและวิดีโอพร้อมกันได้")
    clean_msg = (message or "").strip()
    clean_link = (link or "").strip()
    clean_place_id = (place_id or "").strip()
    if not clean_msg and not clean_link and not images and not clean_video and not clean_place_id:
        raise ValueError("กรุณากรอกเนื้อหา แนบสื่อ ลิงก์ หรือเช็คอินอย่างน้อย 1 อย่างก่อนบันทึกชุดโพสต์")
    now = _now_iso()
    delay = max(0.0, min(float(comment_delay), 300.0))
    with _conn() as c:
        c.execute(
            """INSERT INTO post_presets(
                   name, message, link, image_paths, video_path, is_reel,
                   watermark_enabled, watermark_text, place_id, place_name,
                   comments_json, comment_delay, created_at, updated_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   message=excluded.message,
                   link=excluded.link,
                   image_paths=excluded.image_paths,
                   video_path=excluded.video_path,
                   is_reel=excluded.is_reel,
                   watermark_enabled=excluded.watermark_enabled,
                   watermark_text=excluded.watermark_text,
                   place_id=excluded.place_id,
                   place_name=excluded.place_name,
                   comments_json=excluded.comments_json,
                   comment_delay=excluded.comment_delay,
                   updated_at=excluded.updated_at""",
            (
                clean_name,
                clean_msg,
                clean_link,
                json.dumps(images, ensure_ascii=False),
                clean_video,
                1 if (is_reel and clean_video) else 0,
                1 if watermark_enabled else 0,
                (watermark_text or "").strip(),
                clean_place_id,
                (place_name or "").strip(),
                json.dumps(list(comments or []), ensure_ascii=False),
                delay,
                now,
                now,
            ),
        )
        row = c.execute("SELECT id FROM post_presets WHERE name=?", (clean_name,)).fetchone()
        return int(row["id"]) if row else 0


def list_post_presets() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM post_presets ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return [_decode_post_preset(r) for r in rows]


def get_post_preset(preset_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM post_presets WHERE id=?", (int(preset_id),)).fetchone()
    return _decode_post_preset(r) if r else None


def delete_post_preset(preset_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM post_presets WHERE id=?", (int(preset_id),))


# --- Keyword Auto-Reply Rules & Deduplication Log ---

def _normalize_keyword_list(keywords: list[str] | str) -> list[str]:
    raw_items: list[str]
    if isinstance(keywords, str):
        raw_items = re.split(r"[,\n\r]+", keywords)
    else:
        raw_items = [str(k) for k in (keywords or [])]
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        kw = item.strip()
        if not kw:
            continue
        low = kw.casefold()
        if low in seen:
            continue
        seen.add(low)
        out.append(kw)
    return out


def _decode_auto_reply_rule(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        raw_kw = json.loads(item.get("keywords_json") or "[]")
        item["keywords"] = [str(k).strip() for k in raw_kw if str(k).strip()]
    except ValueError:
        item["keywords"] = []
    item["match_mode"] = (
        item.get("match_mode")
        if item.get("match_mode") in ("contains", "exact")
        else "contains"
    )
    item["hide_after_reply"] = bool(item.get("hide_after_reply", 0))
    item["enabled"] = bool(item.get("enabled", 1))
    return item


def save_auto_reply_rule(
    *,
    keywords: list[str] | str,
    reply_template: str,
    name: str = "",
    match_mode: str = "contains",
    hide_after_reply: bool = False,
    enabled: bool = True,
    rule_id: int | None = None,
) -> int:
    kw_list = _normalize_keyword_list(keywords)
    if not kw_list:
        raise ValueError("กรุณาระบุคีย์เวิร์ดอย่างน้อย 1 คำ")
    clean_reply = (reply_template or "").strip()
    if not clean_reply:
        raise ValueError("กรุณาระบุข้อความตอบกลับอัตโนมัติ")
    mode = match_mode if match_mode in ("contains", "exact") else "contains"
    clean_name = (name or "").strip() or ", ".join(kw_list[:3])
    now = _now_iso()
    kw_json = json.dumps(kw_list, ensure_ascii=False)
    with _conn() as c:
        if rule_id is not None:
            c.execute(
                """UPDATE auto_reply_rules
                   SET name=?, keywords_json=?, match_mode=?, reply_template=?,
                       hide_after_reply=?, enabled=?, updated_at=?
                   WHERE id=?""",
                (
                    clean_name,
                    kw_json,
                    mode,
                    clean_reply,
                    1 if hide_after_reply else 0,
                    1 if enabled else 0,
                    now,
                    int(rule_id),
                ),
            )
            return int(rule_id)
        cur = c.execute(
            """INSERT INTO auto_reply_rules(
                   name, keywords_json, match_mode, reply_template,
                   hide_after_reply, enabled, created_at, updated_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                clean_name,
                kw_json,
                mode,
                clean_reply,
                1 if hide_after_reply else 0,
                1 if enabled else 0,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def list_auto_reply_rules(*, only_enabled: bool = False) -> list[dict]:
    with _conn() as c:
        if only_enabled:
            rows = c.execute(
                "SELECT * FROM auto_reply_rules WHERE enabled=1 ORDER BY id ASC"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM auto_reply_rules ORDER BY id ASC"
            ).fetchall()
    return [_decode_auto_reply_rule(r) for r in rows]


def get_auto_reply_rule(rule_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM auto_reply_rules WHERE id=?", (int(rule_id),)).fetchone()
    return _decode_auto_reply_rule(r) if r else None


def set_auto_reply_rule_enabled(rule_id: int, enabled: bool) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE auto_reply_rules SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, _now_iso(), int(rule_id)),
        )


def delete_auto_reply_rule(rule_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM auto_reply_rules WHERE id=?", (int(rule_id),))


def has_auto_replied(comment_id: str) -> bool:
    cid = (comment_id or "").strip()
    if not cid:
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM auto_reply_log WHERE comment_id=? LIMIT 1",
            (cid,),
        ).fetchone()
    return row is not None


def record_auto_reply(
    *,
    comment_id: str,
    platform: str,
    page_id: str,
    post_id: str,
    rule_id: int = 0,
    reply_comment_id: str = "",
    commenter_name: str = "",
    comment_text: str = "",
    reply_text: str = "",
) -> None:
    cid = (comment_id or "").strip()
    if not cid:
        return
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO auto_reply_log(
                   comment_id, platform, page_id, post_id, rule_id,
                   reply_comment_id, commenter_name, comment_text, reply_text, replied_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cid,
                platform or "facebook",
                page_id or "",
                post_id or "",
                int(rule_id or 0),
                reply_comment_id or "",
                commenter_name or "",
                (comment_text or "")[:500],
                (reply_text or "")[:500],
                _now_iso(),
            ),
        )
        c.execute(
            """DELETE FROM auto_reply_log
               WHERE comment_id NOT IN (
                   SELECT comment_id FROM auto_reply_log
                   ORDER BY replied_at DESC LIMIT 20000
               )"""
        )


def list_recent_auto_replies(limit: int = 50) -> list[dict]:
    lim = max(1, min(int(limit or 50), 500))
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM auto_reply_log ORDER BY replied_at DESC LIMIT ?",
            (lim,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_auto_replies() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS cnt FROM auto_reply_log").fetchone()
    return int(row["cnt"]) if row else 0
