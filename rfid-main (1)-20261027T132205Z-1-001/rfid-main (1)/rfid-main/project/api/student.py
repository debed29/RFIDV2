"""Student routes: profile, home summary, attendance (read-only), preferences, devices, contacts."""
import re
from datetime import date, datetime, timedelta

from flask import g, jsonify, request

import database as db
from .common import (ApiError, bp, connect, json_body, now_text, require_auth, text_field, to_iso)

PREF_KEYS = ("attendance_alerts", "announcements", "sound", "haptics")
PUSH_TOKEN = r"(ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_\-]{10,100}\]"


# ------------------------------------------------------------------ helpers
def get_prefs(c, user_id):
    r = c.execute("SELECT * FROM notification_prefs WHERE user_id = ?", (user_id,)).fetchone()
    prefs = {k: (True if r is None else bool(r[k])) for k in PREF_KEYS}
    prefs["emergency_alerts"] = True  # safety notices cannot be switched off
    return prefs


def unread_count(c, user_id):
    return c.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
                     (user_id,)).fetchone()[0]


def _profile(c, user_id):
    r = c.execute("""SELECT u.name, p.student_number, p.course_section, p.phone
                     FROM users u LEFT JOIN student_profiles p ON p.user_id = u.id
                     WHERE u.id = ?""", (user_id,)).fetchone()
    return {"name": r["name"], "student_number": r["student_number"], "course_section": r["course_section"],
            "phone": r["phone"], "rfid": {"registered": True, "status": "registered"}}


def _session_status(r, today):
    if r["flag"] == "MISSING_TIME_OUT":
        return "incomplete"
    if r["time_out"]:
        return "complete"
    return "pending" if r["time_in"][:10] == today else "incomplete"


def build_days(rows, today):
    """Group log rows by date. A day is incomplete > pending > complete."""
    by_date = {}
    for r in rows:
        by_date.setdefault(r["time_in"][:10], []).append(r)
    days = []
    for day, rs in by_date.items():
        rs = sorted(rs, key=lambda r: (r["time_in"], r["id"]))
        sessions = [{"id": r["id"], "time_in": to_iso(r["time_in"]), "time_out": to_iso(r["time_out"]),
                     "status": _session_status(r, today)} for r in rs]
        seen = {s["status"] for s in sessions}
        status = "incomplete" if "incomplete" in seen else "pending" if "pending" in seen else "complete"
        days.append({"date": day, "status": status, "time_in": sessions[0]["time_in"],
                     "time_out": sessions[-1]["time_out"], "sessions": sessions})
    return sorted(days, key=lambda d: d["date"], reverse=True)


def _logs_between(c, user_id, start, end):
    lo, hi = f"{start} 00:00:00", f"{end + timedelta(days=1)} 00:00:00"
    return c.execute("""SELECT id, time_in, time_out, flag FROM logs
                        WHERE user_id = ? AND time_in >= ? AND time_in < ? ORDER BY time_in, id""",
                     (user_id, lo, hi)).fetchall()


def _today_str():
    return db.school_now().strftime("%Y-%m-%d")


def today_summary(c, user_id):
    today = _today_str()
    days = build_days(_logs_between(c, user_id, date.fromisoformat(today), date.fromisoformat(today)), today)
    if not days:
        return {"date": today, "status": "pending", "state": "not_in", "expected_scan": "time_in",
                "time_in": None, "time_out": None, "sessions": []}
    d = days[0]
    last = d["sessions"][-1]
    d["state"] = "in" if last["time_out"] is None and last["status"] == "pending" else "out"
    d["expected_scan"] = "time_out" if d["state"] == "in" else None
    return d


def _parse_date(text, name):
    try:
        d = datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_date", f"'{name}' must be YYYY-MM-DD.")
    if not 2000 <= d.year <= 2100:
        raise ApiError(400, "invalid_date", f"'{name}' is out of range.")
    return d


def _query_range():
    f, t, m = request.args.get("from"), request.args.get("to"), request.args.get("month")
    if f or t:
        if not (f and t):
            raise ApiError(400, "invalid_range", "Provide both 'from' and 'to'.")
        start, end = _parse_date(f, "from"), _parse_date(t, "to")
        if end < start or (end - start).days > 366:
            raise ApiError(400, "invalid_range", "Range must be 1-366 days.")
        return start, end
    m = m or db.school_now().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        raise ApiError(400, "invalid_month", "'month' must be YYYY-MM.")
    start = _parse_date(m + "-01", "month")
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, nxt - timedelta(days=1)


# ------------------------------------------------------------------ profile / home
@bp.get("/me")
@require_auth
def me():
    with connect() as c:
        out = _profile(c, g.user_id)
        out["preferences"] = get_prefs(c, g.user_id)
        out["unread_count"] = unread_count(c, g.user_id)
    return jsonify(out)


@bp.get("/home")
@require_auth
def home():
    today = _today_str()
    with connect() as c:
        recent = c.execute("""SELECT id, time_in, time_out, flag FROM logs
                              WHERE user_id = ? AND time_in IS NOT NULL ORDER BY id DESC LIMIT 40""",
                           (g.user_id,)).fetchall()
        out = {"profile": _profile(c, g.user_id), "today": today_summary(c, g.user_id),
               "recent": build_days(recent, today)[:5], "unread_count": unread_count(c, g.user_id)}
    return jsonify(out)


@bp.patch("/me/preferences")
@require_auth
def update_preferences():
    d = json_body()
    unknown = set(d) - set(PREF_KEYS)
    if unknown or not d:
        raise ApiError(400, "invalid_field", f"Allowed fields: {', '.join(PREF_KEYS)}.")
    if not all(isinstance(v, bool) for v in d.values()):
        raise ApiError(400, "invalid_field", "Preference values must be true or false.")
    with connect(write=True) as c:
        c.execute("INSERT OR IGNORE INTO notification_prefs (user_id) VALUES (?)", (g.user_id,))
        for k, v in d.items():
            c.execute(f"UPDATE notification_prefs SET {k} = ? WHERE user_id = ?", (int(v), g.user_id))
        prefs = get_prefs(c, g.user_id)
    return jsonify(preferences=prefs)


# ------------------------------------------------------------------ attendance (read-only)
@bp.get("/attendance/today")
@require_auth
def attendance_today():
    with connect() as c:
        return jsonify(today_summary(c, g.user_id))


@bp.get("/attendance")
@require_auth
def attendance_list():
    start, end = _query_range()
    today = _today_str()
    with connect() as c:
        days = build_days(_logs_between(c, g.user_id, start, end), today)
    summary = {k: sum(1 for d in days if d["status"] == k) for k in ("complete", "incomplete", "pending")}
    return jsonify(**{"from": start.isoformat(), "to": end.isoformat()}, days=days, summary=summary)


@bp.get("/attendance/<day>")
@require_auth
def attendance_day(day):
    d = _parse_date(day, "date")
    with connect() as c:
        days = build_days(_logs_between(c, g.user_id, d, d), _today_str())
    if days:
        return jsonify(days[0])
    return jsonify(date=d.isoformat(), status="unavailable", time_in=None, time_out=None, sessions=[])


# ------------------------------------------------------------------ push devices
@bp.post("/devices")
@require_auth
def register_device():
    d = json_body()
    token = text_field(d, "token", min_len=20, max_len=140, pattern=PUSH_TOKEN)
    platform = text_field(d, "platform", min_len=3, max_len=7)
    if platform not in ("android", "ios"):
        raise ApiError(400, "invalid_field", "'platform' must be android or ios.")
    now = now_text()
    with connect(write=True) as c:
        cur = c.execute("""UPDATE push_devices SET user_id = ?, platform = ?, last_seen = ?, disabled_at = NULL
                           WHERE token = ?""", (g.user_id, platform, now, token))
        if cur.rowcount == 0:
            c.execute("""INSERT INTO push_devices (user_id, token, platform, created_at, last_seen)
                         VALUES (?, ?, ?, ?, ?)""", (g.user_id, token, platform, now, now))
        c.execute("""UPDATE push_devices SET disabled_at = ? WHERE user_id = ? AND disabled_at IS NULL AND id NOT IN
                     (SELECT id FROM push_devices WHERE user_id = ? AND disabled_at IS NULL
                      ORDER BY last_seen DESC, id DESC LIMIT 5)""", (now, g.user_id, g.user_id))
    return jsonify(success=True)


@bp.post("/devices/unregister")
@require_auth
def unregister_device():
    token = text_field(json_body(), "token", min_len=20, max_len=140, pattern=PUSH_TOKEN)
    with connect(write=True) as c:
        c.execute("UPDATE push_devices SET disabled_at = ? WHERE token = ? AND user_id = ?",
                  (now_text(), token, g.user_id))
    return jsonify(success=True)


# ------------------------------------------------------------------ faculty contacts
@bp.get("/contacts")
@require_auth
def contacts():
    with connect() as c:
        rows = c.execute("""SELECT id, role, name, phone, notes, updated_at FROM faculty_contacts
                            WHERE active = 1 ORDER BY sort_order, id""").fetchall()
    items = [{"id": r["id"], "role": r["role"], "name": r["name"], "phone": r["phone"],
              "notes": r["notes"], "updated_at": to_iso(r["updated_at"])} for r in rows]
    return jsonify(items=items, kind="school")
