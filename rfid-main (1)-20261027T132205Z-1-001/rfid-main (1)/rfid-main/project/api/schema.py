"""Additive schema for the mobile API. Never alters users/logs/events (except one index)."""
from .common import connect

DDL = [
    """CREATE TABLE IF NOT EXISTS student_profiles (
        user_id INTEGER PRIMARY KEY,
        student_number TEXT NOT NULL UNIQUE COLLATE NOCASE,
        course_section TEXT,
        phone TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS student_accounts (
        user_id INTEGER PRIMARY KEY,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS claim_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        code_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        used_at TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_claim_codes_user ON claim_codes (user_id)",
    """CREATE TABLE IF NOT EXISTS api_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        access_hash TEXT NOT NULL UNIQUE,
        refresh_hash TEXT NOT NULL UNIQUE,
        prev_refresh_hash TEXT,
        access_expires TEXT NOT NULL,
        refresh_expires TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON api_sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_prev ON api_sessions (prev_refresh_hash)",
    """CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        category TEXT NOT NULL CHECK (category IN ('attendance', 'emergency', 'system')),
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        read_at TEXT,
        log_id INTEGER,
        dedupe_key TEXT,
        push_status TEXT NOT NULL DEFAULT 'pending',
        push_attempts INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_dedupe
       ON notifications (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL""",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id, id DESC)",
    """CREATE TABLE IF NOT EXISTS notification_prefs (
        user_id INTEGER PRIMARY KEY,
        attendance_alerts INTEGER NOT NULL DEFAULT 1,
        announcements INTEGER NOT NULL DEFAULT 1,
        sound INTEGER NOT NULL DEFAULT 1,
        haptics INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS push_devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT NOT NULL UNIQUE,
        platform TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        disabled_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_push_devices_user ON push_devices (user_id)",
    """CREATE TABLE IF NOT EXISTS push_tickets (
        ticket_id TEXT PRIMARY KEY,
        token TEXT NOT NULL,
        notification_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        checked_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS faculty_contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        notes TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )""",
    # Helps the per-student history queries; additive and harmless to the RFID flow.
    "CREATE INDEX IF NOT EXISTS idx_logs_user_time ON logs (user_id, time_in)",
]


def init_api_schema():
    with connect() as c:
        for stmt in DDL:
            c.execute(stmt)


def purge_user_data(user_id):
    """Remove every mobile-API row for a deleted user (database.delete_user doesn't know them)."""
    with connect(write=True) as c:
        c.execute("""DELETE FROM push_tickets WHERE notification_id IN
                     (SELECT id FROM notifications WHERE user_id = ?)""", (user_id,))
        for table in ("notifications", "notification_prefs", "push_devices", "api_sessions",
                      "claim_codes", "student_accounts", "student_profiles"):
            c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
