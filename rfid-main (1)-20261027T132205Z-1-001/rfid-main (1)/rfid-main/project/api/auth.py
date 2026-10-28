"""Student authentication: one-time claim codes, login, token refresh, logout."""
import os
import re
import secrets
import sqlite3

from flask import g, jsonify
from werkzeug.security import check_password_hash, generate_password_hash

from .common import (ApiError, STUDENT_NO, bp, client_ip, connect, json_body, later, limiter,
                     now_text, require_auth, sha, text_field)

ACCESS_TTL_MIN = int(os.environ.get("ACCESS_TOKEN_MINUTES", "60"))
REFRESH_TTL_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "30"))
MAX_SESSIONS = 10
MAX_CODE_ATTEMPTS = 5
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DUMMY_HASH = generate_password_hash(secrets.token_hex(8))


def new_claim_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code):
    return re.sub(r"[^A-Za-z0-9]", "", code).upper()


def code_hash(user_id, code):
    return sha(f"{user_id}:{normalize_code(code)}")


def password_field(data, key="password", check_strength=True):
    pw = text_field(data, key, min_len=1, max_len=128, strip=False)
    if check_strength and len(pw) < 8:
        raise ApiError(400, "weak_password", "Password must be at least 8 characters.")
    return pw


def issue_session(c, user_id, session_id=None):
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    access_exp, refresh_exp = later(minutes=ACCESS_TTL_MIN), later(days=REFRESH_TTL_DAYS)
    if session_id is None:
        c.execute("DELETE FROM api_sessions WHERE user_id = ? AND (revoked = 1 OR refresh_expires < ?)",
                  (user_id, now_text()))
        c.execute("""INSERT INTO api_sessions
                     (user_id, access_hash, refresh_hash, access_expires, refresh_expires, created_at)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (user_id, sha(access), sha(refresh), access_exp, refresh_exp, now_text()))
        # Keep only the newest MAX_SESSIONS active sessions per student.
        c.execute("""UPDATE api_sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0 AND id NOT IN
                     (SELECT id FROM api_sessions WHERE user_id = ? AND revoked = 0
                      ORDER BY id DESC LIMIT ?)""", (user_id, user_id, MAX_SESSIONS))
    else:
        c.execute("""UPDATE api_sessions SET prev_refresh_hash = refresh_hash, refresh_hash = ?,
                     access_hash = ?, access_expires = ?, refresh_expires = ? WHERE id = ?""",
                  (sha(refresh), sha(access), access_exp, refresh_exp, session_id))
    return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
            "expires_in": ACCESS_TTL_MIN * 60, "refresh_expires_in": REFRESH_TTL_DAYS * 86400}


def _user_summary(c, user_id):
    r = c.execute("""SELECT u.name, p.student_number FROM users u
                     JOIN student_profiles p ON p.user_id = u.id WHERE u.id = ?""", (user_id,)).fetchone()
    return {"name": r["name"], "student_number": r["student_number"]}


@bp.post("/auth/claim")
def claim_account():
    d = json_body()
    sn = text_field(d, "student_number", min_len=2, max_len=32, pattern=STUDENT_NO)
    code = text_field(d, "claim_code", min_len=8, max_len=12)
    pw = password_field(d)
    ip = client_ip()
    limiter.check(f"claim-ip:{ip}", 30, 600)
    limiter.check(f"claim:{ip}:{sn.lower()}", 8, 600)
    limiter.add(f"claim-ip:{ip}")
    generic = ApiError(400, "invalid_claim",
                       "The student number or claim code is not valid. Ask the school office for a new code.")

    with connect() as c:
        prof = c.execute(
            """SELECT p.user_id, a.user_id AS has_account FROM student_profiles p
               JOIN users u ON u.id = p.user_id AND u.role = 'student' AND u.archived = 0
               LEFT JOIN student_accounts a ON a.user_id = p.user_id
               WHERE p.student_number = ?""", (sn,)).fetchone()
        cc = None
        if prof and not prof["has_account"]:
            cc = c.execute("""SELECT id, code_hash, attempts FROM claim_codes
                              WHERE user_id = ? AND used_at IS NULL AND expires_at > ?
                              ORDER BY id DESC LIMIT 1""", (prof["user_id"], now_text())).fetchone()
    if cc is None or cc["attempts"] >= MAX_CODE_ATTEMPTS:
        limiter.add(f"claim:{ip}:{sn.lower()}")
        raise generic
    user_id = prof["user_id"]
    if not secrets.compare_digest(cc["code_hash"], code_hash(user_id, code)):
        with connect(write=True) as c:
            c.execute("UPDATE claim_codes SET attempts = attempts + 1 WHERE id = ?", (cc["id"],))
        limiter.add(f"claim:{ip}:{sn.lower()}")
        raise generic

    pw_hash = generate_password_hash(pw)  # CPU heavy: done outside the DB lock
    try:
        with connect(write=True) as c:
            used = c.execute("""UPDATE claim_codes SET used_at = ? WHERE id = ? AND used_at IS NULL
                                AND attempts < ? AND expires_at > ?""",
                             (now_text(), cc["id"], MAX_CODE_ATTEMPTS, now_text()))
            if used.rowcount != 1:
                raise generic
            c.execute("INSERT INTO student_accounts (user_id, password_hash, created_at) VALUES (?, ?, ?)",
                      (user_id, pw_hash, now_text()))
            c.execute("INSERT OR IGNORE INTO notification_prefs (user_id) VALUES (?)", (user_id,))
            # OR IGNORE: after an admin reset the old 'welcome' row still exists; don't fail the claim.
            c.execute("""INSERT OR IGNORE INTO notifications (user_id, category, title, body, created_at,
                         dedupe_key, push_status) VALUES (?, 'system', 'Welcome', ?, ?, 'welcome', 'skipped')""",
                      (user_id, "Your account is now active. Attendance updates will appear here.", now_text()))
            tokens = issue_session(c, user_id)
            user = _user_summary(c, user_id)
    except sqlite3.IntegrityError:
        raise generic
    resp = jsonify(**tokens, user=user)
    resp.status_code = 201
    return resp


@bp.post("/auth/login")
def login():
    d = json_body()
    sn = text_field(d, "student_number", min_len=2, max_len=32, pattern=STUDENT_NO)
    pw = password_field(d, check_strength=False)
    ip = client_ip()
    limiter.check(f"login-ip:{ip}", 40, 600)
    limiter.check(f"login:{ip}:{sn.lower()}", 8, 600)
    limiter.add(f"login-ip:{ip}")
    with connect() as c:
        row = c.execute(
            """SELECT a.user_id, a.password_hash FROM student_profiles p
               JOIN student_accounts a ON a.user_id = p.user_id
               JOIN users u ON u.id = p.user_id AND u.role = 'student' AND u.archived = 0
               WHERE p.student_number = ?""", (sn,)).fetchone()
    ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, pw) and row is not None
    if not ok:
        limiter.add(f"login:{ip}:{sn.lower()}")
        raise ApiError(401, "invalid_credentials", "Incorrect student number or password.")
    with connect(write=True) as c:
        tokens = issue_session(c, row["user_id"])
        user = _user_summary(c, row["user_id"])
    return jsonify(**tokens, user=user)


@bp.post("/auth/refresh")
def refresh():
    d = json_body()
    token = text_field(d, "refresh_token", min_len=20, max_len=200)
    limiter.check(f"refresh-ip:{client_ip()}", 60, 600)
    limiter.add(f"refresh-ip:{client_ip()}")
    h = sha(token)
    invalid = ApiError(401, "invalid_refresh", "Please sign in again.")
    with connect(write=True) as c:
        row = c.execute("""SELECT s.id, s.user_id, s.revoked, s.refresh_expires, s.refresh_hash FROM api_sessions s
                           WHERE s.refresh_hash = ? OR s.prev_refresh_hash = ?""", (h, h)).fetchone()
        if row is None:
            raise invalid
        if row["refresh_hash"] != h:
            # An already-rotated token was replayed: assume theft and kill this session.
            c.execute("UPDATE api_sessions SET revoked = 1 WHERE id = ?", (row["id"],))
            c.execute("COMMIT")  # keep the revocation even though we raise below
            c.execute("BEGIN IMMEDIATE")
            raise invalid
        active = c.execute("""SELECT 1 FROM student_accounts a JOIN users u ON u.id = a.user_id
                              WHERE a.user_id = ? AND u.archived = 0 AND u.role = 'student'""",
                           (row["user_id"],)).fetchone()
        if row["revoked"] or row["refresh_expires"] < now_text() or not active:
            raise invalid
        tokens = issue_session(c, row["user_id"], session_id=row["id"])
    return jsonify(**tokens)


@bp.post("/auth/logout")
@require_auth
def logout():
    d = json_body() if _has_body() else {}
    with connect(write=True) as c:
        c.execute("UPDATE api_sessions SET revoked = 1 WHERE id = ?", (g.session_id,))
        tok = d.get("push_token")
        if isinstance(tok, str):
            c.execute("UPDATE push_devices SET disabled_at = ? WHERE token = ? AND user_id = ?",
                      (now_text(), tok, g.user_id))
    return jsonify(success=True)


def _has_body():
    from flask import request
    return bool(request.get_data(cache=True))
