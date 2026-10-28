"""Admin routes (X-Admin-Key header). Block /api/v1/admin/* at any public proxy or tunnel."""
import sqlite3
from datetime import timedelta

from flask import jsonify, request

from . import notifier
from .auth import code_hash, new_claim_code
from .common import (ApiError, PHONE, STUDENT_NO, bp, connect, json_body, later, now_dt, now_text,
                     require_admin, text_field, to_iso)


def _active_student(c, user_id):
    r = c.execute("SELECT id, name FROM users WHERE id = ? AND role = 'student' AND archived = 0",
                  (user_id,)).fetchone()
    if r is None:
        raise ApiError(404, "not_found", "No such active student.")
    return r


@bp.get("/admin/students")
@require_admin
def admin_students():
    q = (request.args.get("q") or "").strip()[:50]
    with connect() as c:
        rows = c.execute(
            """SELECT u.id, u.name, p.student_number, p.course_section, p.phone,
                      a.user_id IS NOT NULL AS claimed
               FROM users u LEFT JOIN student_profiles p ON p.user_id = u.id
               LEFT JOIN student_accounts a ON a.user_id = u.id
               WHERE u.role = 'student' AND u.archived = 0
                 AND (? = '' OR u.name LIKE '%' || ? || '%' OR p.student_number LIKE '%' || ? || '%')
               ORDER BY u.name LIMIT 500""", (q, q, q)).fetchall()
    return jsonify(items=[{"user_id": r["id"], "name": r["name"], "student_number": r["student_number"],
                           "course_section": r["course_section"], "phone": r["phone"],
                           "has_profile": r["student_number"] is not None, "claimed": bool(r["claimed"])}
                          for r in rows])


@bp.put("/admin/students/<int:user_id>/profile")
@require_admin
def admin_set_profile(user_id):
    d = json_body()
    sn = text_field(d, "student_number", min_len=2, max_len=32, pattern=STUDENT_NO)
    section = text_field(d, "course_section", max_len=60, required=False)
    phone = text_field(d, "phone", max_len=25, required=False, pattern=PHONE)
    try:
        with connect(write=True) as c:
            _active_student(c, user_id)
            c.execute("""INSERT INTO student_profiles (user_id, student_number, course_section, phone, updated_at)
                         VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
                         student_number = excluded.student_number, course_section = excluded.course_section,
                         phone = excluded.phone, updated_at = excluded.updated_at""",
                      (user_id, sn, section, phone, now_text()))
    except sqlite3.IntegrityError:
        raise ApiError(409, "duplicate_student_number", "That student number is already assigned.")
    return jsonify(success=True)


@bp.post("/admin/students/<int:user_id>/claim-code")
@require_admin
def admin_claim_code(user_id):
    d = request.get_json(silent=True) or {}
    hours = d.get("expires_hours", 72) if isinstance(d, dict) else 72
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 168:
        raise ApiError(400, "invalid_field", "'expires_hours' must be 1-168.")
    code = new_claim_code()
    with connect(write=True) as c:
        _active_student(c, user_id)
        prof = c.execute("SELECT student_number FROM student_profiles WHERE user_id = ?", (user_id,)).fetchone()
        if prof is None:
            raise ApiError(409, "profile_required", "Set the student's profile (student number) first.")
        if c.execute("SELECT 1 FROM student_accounts WHERE user_id = ?", (user_id,)).fetchone():
            raise ApiError(409, "already_claimed", "This student already has an account. Reset it first.")
        c.execute("UPDATE claim_codes SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now_text(), user_id))
        expires = (now_dt() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO claim_codes (user_id, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                  (user_id, code_hash(user_id, code), expires, now_text()))
    # The plaintext code is only ever returned here; only its hash is stored.
    return jsonify(claim_code=code, student_number=prof["student_number"], expires_at=to_iso(expires)), 201


@bp.post("/admin/students/<int:user_id>/reset-account")
@require_admin
def admin_reset_account(user_id):
    """Forgot-password path: removes the account and sessions so a new claim code can be issued."""
    with connect(write=True) as c:
        _active_student(c, user_id)
        for table in ("api_sessions", "push_devices", "student_accounts", "notification_prefs"):
            c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        c.execute("UPDATE claim_codes SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now_text(), user_id))
    return jsonify(success=True)


# ------------------------------------------------------------------ faculty contacts
def _contact_fields(d, partial):
    out = {}
    rules = {"role": dict(max_len=60), "name": dict(max_len=100), "phone": dict(max_len=25, pattern=PHONE),
             "notes": dict(max_len=200, min_len=0)}
    for key, kw in rules.items():
        if key in d or (not partial and key != "notes"):
            out[key] = text_field(d, key, required=not (partial or key == "notes"), **kw)
    if "sort_order" in d:
        if not isinstance(d["sort_order"], int) or isinstance(d["sort_order"], bool):
            raise ApiError(400, "invalid_field", "'sort_order' must be a whole number.")
        out["sort_order"] = d["sort_order"]
    if "active" in d:
        if not isinstance(d["active"], bool):
            raise ApiError(400, "invalid_field", "'active' must be true or false.")
        out["active"] = int(d["active"])
    return out


@bp.get("/admin/contacts")
@require_admin
def admin_contacts():
    with connect() as c:
        rows = c.execute("SELECT * FROM faculty_contacts ORDER BY sort_order, id").fetchall()
    return jsonify(items=[dict(r) for r in rows])


@bp.post("/admin/contacts")
@require_admin
def admin_contact_create():
    f = _contact_fields(json_body(), partial=False)
    with connect(write=True) as c:
        cur = c.execute("""INSERT INTO faculty_contacts (role, name, phone, notes, sort_order, active, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (f["role"], f["name"], f["phone"], f.get("notes"), f.get("sort_order", 0),
                         f.get("active", 1), now_text()))
    return jsonify(id=cur.lastrowid), 201


@bp.patch("/admin/contacts/<int:cid>")
@require_admin
def admin_contact_update(cid):
    f = _contact_fields(json_body(), partial=True)
    if not f:
        raise ApiError(400, "invalid_body", "Nothing to update.")
    with connect(write=True) as c:
        sets = ", ".join(f"{k} = ?" for k in f)
        n = c.execute(f"UPDATE faculty_contacts SET {sets}, updated_at = ? WHERE id = ?",
                      [*f.values(), now_text(), cid]).rowcount
    if n == 0:
        raise ApiError(404, "not_found", "No such contact.")
    return jsonify(success=True)


@bp.delete("/admin/contacts/<int:cid>")
@require_admin
def admin_contact_delete(cid):
    with connect(write=True) as c:
        n = c.execute("UPDATE faculty_contacts SET active = 0, updated_at = ? WHERE id = ?",
                      (now_text(), cid)).rowcount
    if n == 0:
        raise ApiError(404, "not_found", "No such contact.")
    return jsonify(success=True)


# ------------------------------------------------------------------ announcements
@bp.post("/admin/announcements")
@require_admin
def admin_announce():
    d = json_body()
    category = text_field(d, "category", min_len=6, max_len=9)
    if category not in ("emergency", "system"):
        raise ApiError(400, "invalid_field", "'category' must be emergency or system.")
    title = text_field(d, "title", max_len=80)
    body = text_field(d, "body", max_len=500)
    return jsonify(recipients=notifier.create_announcement(category, title, body)), 201
