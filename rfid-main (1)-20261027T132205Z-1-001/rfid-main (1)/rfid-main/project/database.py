import math
import sqlite3
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
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rfid_uid TEXT NOT NULL,
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
        user_columns = {
            "time_limit_minutes": "INTEGER",
            "is_expired": "INTEGER NOT NULL DEFAULT 0",
            "archived": "INTEGER NOT NULL DEFAULT 0",
            "student_number": "TEXT",
            "surname": "TEXT",
            "first_name": "TEXT",
            "middle_name": "TEXT",
            "last_successful_tap": "REAL",
        }
        for column, definition in user_columns.items():
            if column not in existing_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        _migrate_user_uid_uniqueness(conn)
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
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS unique_active_student_number
            ON users (student_number)
            WHERE student_number IS NOT NULL AND archived = 0
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS unique_active_rfid_uid
            ON users (rfid_uid) WHERE archived = 0
        """)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _migrate_user_uid_uniqueness(conn):
    unique_uid_index = False
    for index in conn.execute("PRAGMA index_list(users)"):
        if not index["unique"]:
            continue
        columns = [row["name"] for row in conn.execute(
            f"PRAGMA index_info({index['name']})"
        )]
        if columns == ["rfid_uid"]:
            unique_uid_index = True
            break
    if not unique_uid_index:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE users_rebuilt (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rfid_uid TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('student', 'visitor')),
                is_temp INTEGER NOT NULL DEFAULT 0,
                time_limit_minutes INTEGER,
                is_expired INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                registered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                student_number TEXT,
                surname TEXT,
                first_name TEXT,
                middle_name TEXT,
                last_successful_tap REAL
            )
        """)
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
        column_list = ", ".join(f'"{column}"' for column in columns)
        conn.execute(
            f"INSERT INTO users_rebuilt ({column_list}) "
            f"SELECT {column_list} FROM users"
        )
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_rebuilt RENAME TO users")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def uid_exists(uid, include_archived=False):
    conn = get_conn()
    query = "SELECT * FROM users WHERE rfid_uid = ?"
    if not include_archived:
        query += " AND archived = 0"
    query += " ORDER BY archived ASC, id DESC LIMIT 1"
    row = conn.execute(query, (uid,)).fetchone()
    conn.close()
    return row

def student_number_exists(student_number, exclude_uid=None):
    if not student_number:
        return False
    conn = get_conn()
    if exclude_uid:
        row = conn.execute(
            """SELECT 1 FROM users
               WHERE student_number = ? COLLATE NOCASE AND rfid_uid <> ?
                 AND archived = 0 LIMIT 1""",
            (student_number, exclude_uid),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT 1 FROM users
               WHERE student_number = ? COLLATE NOCASE AND archived = 0 LIMIT 1""",
            (student_number,),
        ).fetchone()
    conn.close()
    return row is not None

def add_user(uid, name, role, is_temp=0, time_limit_minutes=None, surname=None,
             first_name=None, middle_name=None, student_number=None):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO users
               (rfid_uid, name, role, is_temp, time_limit_minutes, student_number,
                surname, first_name, middle_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, name, role, is_temp, time_limit_minutes, student_number,
             surname, first_name, middle_name),
        )
        conn.execute("COMMIT")
        return student_number
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

def upgrade_temp_user(uid, name, role, time_limit_minutes=None, surname=None,
                      first_name=None, middle_name=None, student_number=None):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if time_limit_minutes is None:
            conn.execute(
                """UPDATE users SET name = ?, role = ?, is_temp = 0, student_number = ?,
                   surname = ?, first_name = ?, middle_name = ?
                   WHERE rfid_uid = ?""",
                (name, role, student_number, surname, first_name, middle_name, uid),
            )
        else:
            conn.execute(
                """UPDATE users SET name = ?, role = ?, is_temp = 0, time_limit_minutes = ?,
                   student_number = ?, surname = ?, first_name = ?, middle_name = ?
                   WHERE rfid_uid = ?""",
                (name, role, time_limit_minutes, student_number, surname, first_name, middle_name, uid),
            )
        conn.execute("COMMIT")
        return student_number
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def update_user_name(user_id, surname, first_name, middle_name):
    parts = [surname, first_name, middle_name]
    name = " ".join(part for part in parts if part)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = conn.execute(
            """UPDATE users SET surname = ?, first_name = ?, middle_name = ?, name = ?
               WHERE id = ? AND role = 'student' AND archived = 0""",
            (surname, first_name, middle_name, name, user_id),
        )
        if result.rowcount == 0:
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def expire_visitor_card(uid):
    """Mark a visitor card as expired after a complete visit (time in + time out)."""
    conn = get_conn()
    conn.execute("UPDATE users SET is_expired = 1 WHERE rfid_uid = ? AND role = 'visitor'", (uid,))
    conn.close()


def delete_user(uid):
    """Deactivate a card while retaining its UID and attendance history."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE users SET archived = 1, is_expired = 1
               WHERE rfid_uid = ? AND archived = 0""",
            (uid,),
        )
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
    users = [dict(row) for row in rows]
    for user in users:
        surname, first_name, middle_name = user_name_parts(user)
        user["surname"] = surname
        user["first_name"] = first_name
        user["middle_name"] = middle_name
        user["name"] = format_user_name(user)
    return users


def user_name_parts(user):
    if user["role"] not in ("student", "visitor"):
        return "", "", ""

    keys = user.keys()
    surname = user["surname"] if "surname" in keys else None
    first_name = user["first_name"] if "first_name" in keys else None
    middle_name = user["middle_name"] if "middle_name" in keys else None
    if not any((surname, first_name, middle_name)):
        parts = (user["name"] or "").split()
        if parts:
            surname = parts[-1] if len(parts) > 1 else ""
            first_name = parts[0]
            middle_name = " ".join(parts[1:-1])
    return surname or "", first_name or "", middle_name or ""


def format_user_name(user):
    if user["role"] not in ("student", "visitor"):
        return user["name"]
    surname, first_name, middle_name = user_name_parts(user)
    if not surname or not first_name:
        return user["name"]
    return " ".join(part for part in (surname, first_name, middle_name) if part)


def get_visitor_time_limit_minutes(uid):
    """Return the time_limit_minutes for a visitor card, or None."""
    conn = get_conn()
    row = conn.execute(
        """SELECT time_limit_minutes FROM users
           WHERE rfid_uid = ? AND role = 'visitor' AND archived = 0""",
        (uid,),
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

    Returns ``(result_code, user_row, log_id, cooldown_seconds)`` after commit.
    ``log_id`` is the exact attendance log created or affected by the tap, or
    ``None`` when no attendance log was involved.  Callers can safely use it
    for post-commit work without guessing which log is newest.

    ``limit_hours`` may be None when the card has its own time_limit_minutes.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT * FROM users WHERE rfid_uid = ? AND archived = 0", (uid,)
        ).fetchone()
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

        conn.execute(
            "UPDATE users SET last_successful_tap = ? WHERE id = ?", (now_epoch, user["id"])
        )

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
        SELECT u.name, u.role, u.student_number, u.surname, u.first_name, u.middle_name,
               l.time_in, l.time_out, l.flag
        FROM logs l JOIN users u ON u.id = l.user_id
        ORDER BY l.id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    logs = [dict(row) for row in rows]
    for log in logs:
        log["name"] = format_user_name(log)
    return logs