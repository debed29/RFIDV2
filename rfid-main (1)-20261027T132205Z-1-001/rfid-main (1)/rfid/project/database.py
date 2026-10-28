import sqlite3
import math
from time import time as unix_time
from datetime import datetime, timedelta, timezone

DB_NAME = "rfid_system.db"
SCHOOL_TZ = timezone(timedelta(hours=8))
TAP_COOLDOWN_SECONDS = 30


def school_now():
    return datetime.now(SCHOOL_TZ)

def get_conn():
    conn = sqlite3.connect(DB_NAME, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rfid_uid TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('student', 'visitor')),
            is_temp INTEGER NOT NULL DEFAULT 0,
            time_limit_minutes INTEGER,
            is_expired INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            registered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            time_in TEXT,
            time_out TEXT,
            expected_out TEXT,
            flag TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(logs)")}
    if "expected_out" not in existing:
        conn.execute("ALTER TABLE logs ADD COLUMN expected_out TEXT")
    if "flag" not in existing:
        conn.execute("ALTER TABLE logs ADD COLUMN flag TEXT")
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "time_limit_minutes" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN time_limit_minutes INTEGER")
    if "is_expired" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_expired INTEGER NOT NULL DEFAULT 0")
    if "archived" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "last_successful_tap" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_successful_tap REAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_record
        ON logs (user_id)
        WHERE time_out IS NULL AND (flag IS NULL OR flag = 'OVERSTAY')
    """)
    conn.close()

def uid_exists(uid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE rfid_uid = ?", (uid,)).fetchone()
    conn.close()
    return row

def add_user(uid, name, role, is_temp=0, time_limit_minutes=None):
    conn = get_conn()
    conn.execute("INSERT INTO users (rfid_uid, name, role, is_temp, time_limit_minutes) VALUES (?, ?, ?, ?, ?)",
                 (uid, name, role, is_temp, time_limit_minutes))
    conn.close()

def upgrade_temp_user(uid, name, role, time_limit_minutes=None):
    conn = get_conn()
    if time_limit_minutes is None:
        conn.execute("UPDATE users SET name = ?, role = ?, is_temp = 0 WHERE rfid_uid = ?",
                     (name, role, uid))
    else:
        conn.execute("UPDATE users SET name = ?, role = ?, is_temp = 0, time_limit_minutes = ? WHERE rfid_uid = ?",
                     (name, role, time_limit_minutes, uid))
    conn.close()


def expire_visitor_card(uid):
    """Mark a visitor card as expired after a complete visit (time in + time out)."""
    conn = get_conn()
    conn.execute("UPDATE users SET is_expired = 1 WHERE rfid_uid = ? AND role = 'visitor'", (uid,))
    conn.close()


def re_register_visitor_card(uid, name, role, time_limit_minutes=None):
    """Re-register any visitor card for a new visitor.

    The old user row is archived (uid rewritten, archived=1) so every past
    attendance record keeps the original visitor's name. Any open visit is
    flagged MISSING_TIME_OUT because that visitor never timed out. A brand new
    row is inserted under the real card UID so the card can be used again.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT id FROM users WHERE rfid_uid = ? AND archived = 0", (uid,)).fetchone()
        if old is None:
            conn.execute("ROLLBACK")
            return False
        # Close any open visit: that visitor's card is being handed to someone new.
        conn.execute("""
            UPDATE logs SET flag = 'MISSING_TIME_OUT'
            WHERE user_id = ? AND time_out IS NULL AND (flag IS NULL OR flag = 'OVERSTAY')
        """, (old["id"],))
        # Archive the old row so history keeps the old name.
        conn.execute("UPDATE users SET rfid_uid = ?, archived = 1, is_expired = 1 WHERE id = ?",
                     (f"{uid}#USED{old['id']}", old["id"]))
        conn.execute(
            "INSERT INTO users (rfid_uid, name, role, is_temp, time_limit_minutes, is_expired, archived) "
            "VALUES (?, ?, ?, 0, ?, 0, 0)",
            (uid, name, role, time_limit_minutes),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

def delete_user(uid):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT id FROM users WHERE rfid_uid = ?", (uid,)).fetchone()
        if user:
            conn.execute("DELETE FROM logs WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM events WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users WHERE archived = 0 ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_visitor_time_limit_minutes(uid):
    """Return the time_limit_minutes for a visitor card, or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT time_limit_minutes FROM users WHERE rfid_uid = ? AND role = 'visitor'", (uid,)
    ).fetchone()
    conn.close()
    if row and row["time_limit_minutes"] is not None:
        return row["time_limit_minutes"]
    return None

def open_log_exists(user_id):
    conn = get_conn()
    row = conn.execute("SELECT id FROM logs WHERE user_id = ? AND time_out IS NULL",
                       (user_id,)).fetchone()
    conn.close()
    return row is not None


def record_tap(uid, limit_hours=None):
    """Apply one tap atomically.

    Returns ``(result_code, user_row, log_id)`` after the transaction commits.
    ``log_id`` is the exact attendance log created or affected by the tap, or
    ``None`` when no attendance log was involved.  Callers can safely use it
    for post-commit work without guessing which log is newest.

    ``limit_hours`` may be None when the card has its own time_limit_minutes.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM users WHERE rfid_uid = ?", (uid,)).fetchone()
        if user is None:
            conn.execute("COMMIT")
            return "DENY_UNKNOWN", None, None, None

        if user["is_expired"] and user["role"] == "visitor":
            conn.execute("COMMIT")
            return "REJECT_EXPIRED_VISITOR", user, None, None

        now = school_now()
        now_epoch = unix_time()
        last_tap = user["last_successful_tap"]
        if last_tap is not None:
            remaining = TAP_COOLDOWN_SECONDS - (now_epoch - last_tap)
            if remaining > 0:
                conn.execute("COMMIT")
                return "REJECT_COOLDOWN", user, None, math.ceil(remaining)
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        today = now.strftime("%Y-%m-%d")
        conn.execute("""
            UPDATE logs SET flag = 'MISSING_TIME_OUT'
            WHERE user_id = ? AND time_out IS NULL
              AND (flag IS NULL OR flag = 'OVERSTAY')
              AND substr(time_in, 1, 10) < ?
        """, (user["id"], today))

        open_record = conn.execute("""
            SELECT * FROM logs
            WHERE user_id = ? AND time_out IS NULL
              AND (flag IS NULL OR flag = 'OVERSTAY')
        """, (user["id"],)).fetchone()
        log_id = None

        if open_record is None:
            expected_out = None
            if user["role"] == "visitor":
                # Use per-card time_limit_minutes if available, otherwise fall back to limit_hours
                if user["time_limit_minutes"] is not None:
                    expected_out = (now + timedelta(minutes=user["time_limit_minutes"])).strftime("%Y-%m-%d %H:%M:%S")
                elif limit_hours is not None:
                    expected_out = (now + timedelta(hours=limit_hours)).strftime("%Y-%m-%d %H:%M:%S")
            log_id = conn.execute(
                "INSERT INTO logs (user_id, time_in, expected_out) VALUES (?, ?, ?)",
                (user["id"], now_text, expected_out),
            ).lastrowid
            result = "TIME_IN_OK"
        else:
            log_id = open_record["id"]
            flag = open_record["flag"]
            if open_record["expected_out"] and now_text > open_record["expected_out"]:
                flag = "OVERSTAY"
            conn.execute("UPDATE logs SET time_out = ?, flag = ? WHERE id = ?",
                         (now_text, flag, open_record["id"]))
            result = "TIME_OUT_OK"
            if user["role"] == "visitor":
                conn.execute("UPDATE users SET is_expired = 1 WHERE id = ?", (user["id"],))

        conn.execute("UPDATE users SET last_successful_tap = ? WHERE id = ?",
                     (now_epoch, user["id"]))

        conn.execute("COMMIT")
        return result, user, log_id, None
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def count_today():
    today = school_now().strftime("%Y-%m-%d")
    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE substr(time_in, 1, 10) = ?", (today,)
    ).fetchone()[0]
    conn.close()
    return count

def log_time_in(user_id):
    conn = get_conn()
    conn.execute("INSERT INTO logs (user_id, time_in) VALUES (?, datetime('now','localtime'))",
                 (user_id,))
    conn.commit()
    conn.close()

def log_time_out(user_id):
    conn = get_conn()
    conn.execute("UPDATE logs SET time_out = datetime('now','localtime') "
                 "WHERE user_id = ? AND time_out IS NULL", (user_id,))
    conn.commit()
    conn.close()

def get_recent_logs(limit=30):
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.name, u.role, l.time_in, l.time_out, l.flag
        FROM logs l JOIN users u ON u.id = l.user_id
        ORDER BY l.id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
