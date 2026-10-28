"""Creates inbox notifications from confirmed taps and announcements."""
import logging
import secrets
import sqlite3
import time

from . import push
from .common import clock, connect, later, now_text

log = logging.getLogger("api.notifier")

THROTTLE_MINUTES = 10  # at most one duplicate-scan / failed-scan warning per student per window


def notify_after_tap(code, user, log_id=None):
    """Called by /tap after record_tap() has committed. Never raises, never blocks."""
    try:
        if user is None or user["role"] != "student":
            return
        if code in ("TIME_IN_OK", "TIME_OUT_OK", "ALREADY_CHECKED_IN", "REJECT_NO_OPEN_RECORD"):
            push.submit("tap", code=code, user_id=user["id"], log_id=log_id)
    except Exception:
        log.exception("could not queue notification for tap")


def create_notification(c, user_id, category, title, body, dedupe_key=None, log_id=None):
    """Insert one inbox row. Returns its id, or None when dedupe_key already exists."""
    try:
        cur = c.execute(
            """INSERT INTO notifications (user_id, category, title, body, created_at, log_id, dedupe_key)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, category, title, body, now_text(), log_id, dedupe_key))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def _throttled(c, user_id, prefix):
    return c.execute("SELECT 1 FROM notifications WHERE user_id = ? AND dedupe_key LIKE ? AND created_at >= ?",
                     (user_id, prefix + ":%", later(minutes=-THROTTLE_MINUTES))).fetchone() is not None


def process_tap(code, user_id, log_id=None):
    """Turn one confirmed tap result into an inbox row. Returns the notification id or None."""
    with connect(write=True) as c:
        if not c.execute("SELECT 1 FROM student_accounts WHERE user_id = ?", (user_id,)).fetchone():
            return None

        if code == "TIME_IN_OK":
            if log_id is None:
                return None
            r = c.execute("""SELECT id, time_in FROM logs
                             WHERE id = ? AND user_id = ? AND time_in IS NOT NULL""",
                          (log_id, user_id)).fetchone()
            if r is None or not r["time_in"]:
                return None
            return create_notification(
                c, user_id, "attendance", "Attendance Recorded",
                f"Your time in has been recorded successfully at {clock(r['time_in'])}.",
                f"log:{r['id']}:IN", r["id"])

        if code == "TIME_OUT_OK":
            if log_id is None:
                return None
            r = c.execute("""SELECT id, time_out FROM logs
                             WHERE id = ? AND user_id = ? AND time_out IS NOT NULL""",
                          (log_id, user_id)).fetchone()
            if r is None:
                return None
            return create_notification(
                c, user_id, "attendance", "Attendance Recorded",
                f"Your time out has been recorded successfully at {clock(r['time_out'])}.",
                f"log:{r['id']}:OUT", r["id"])

        if code == "ALREADY_CHECKED_IN":
            if _throttled(c, user_id, "dup"):
                return None
            r = c.execute("""SELECT time_in FROM logs WHERE user_id = ? AND time_out IS NULL
                             ORDER BY id DESC LIMIT 1""", (user_id,)).fetchone()
            when = f" at {clock(r['time_in'])}" if r and r["time_in"] else ""
            return create_notification(
                c, user_id, "attendance", "Duplicate Scan",
                f"You already scanned{when}. Wait for the cooldown before scanning again.",
                f"dup:{time.time_ns()}")

        if code == "REJECT_NO_OPEN_RECORD":
            if _throttled(c, user_id, "noout"):
                return None
            return create_notification(
                c, user_id, "attendance", "Attendance Issue",
                "A time out was not recorded because there is no time in on record. Please see school staff.",
                f"noout:{time.time_ns()}")
    return None


def create_announcement(category, title, body):
    """Insert one inbox row for every active student account, then queue pushes. Returns the count."""
    key = f"ann:{secrets.token_hex(8)}"
    ids = []
    with connect(write=True) as c:
        users = c.execute("""SELECT a.user_id FROM student_accounts a JOIN users u ON u.id = a.user_id
                             WHERE u.archived = 0 AND u.role = 'student'""").fetchall()
        for u in users:
            nid = create_notification(c, u["user_id"], category, title, body, key)
            if nid:
                ids.append(nid)
    for nid in ids:
        push.submit("push", nid=nid)
    return len(ids)
