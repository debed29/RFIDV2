"""Shared helpers for the /api/v1 blueprint: errors, DB access, auth, rate limiting."""
import hashlib
import hmac
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, g, jsonify, request
from werkzeug.exceptions import HTTPException

import database as db

log = logging.getLogger("api")
bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- errors
class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.headers = headers or {}


@bp.errorhandler(Exception)
def _on_error(exc):
    if isinstance(exc, ApiError):
        resp = jsonify(error={"code": exc.code, "message": exc.message})
        resp.status_code = exc.status
        for k, v in exc.headers.items():
            resp.headers[k] = v
        return resp
    if isinstance(exc, HTTPException):
        resp = jsonify(error={"code": (exc.name or "error").lower().replace(" ", "_"),
                              "message": exc.description})
        resp.status_code = exc.code or 500
        return resp
    log.exception("Unhandled error in %s", request.path)
    resp = jsonify(error={"code": "internal_error", "message": "Something went wrong on the server."})
    resp.status_code = 500
    return resp


@bp.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/<path:_rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def _not_found(_rest):
    raise ApiError(404, "not_found", "No such endpoint.")


# ---------------------------------------------------------------- database
@contextmanager
def connect(write=False):
    """Open a connection to the existing SQLite DB. write=True wraps one atomic transaction."""
    conn = db.get_conn()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if write:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- time
def now_dt():
    return db.school_now()


def fmt(dt):
    return dt.strftime(FMT)


def now_text():
    return fmt(now_dt())


def later(**delta):
    return fmt(now_dt() + timedelta(**delta))


def to_iso(text):
    """Naive school-time text -> ISO 8601 with explicit +08:00 offset."""
    if not text:
        return None
    return datetime.strptime(text, FMT).replace(tzinfo=db.SCHOOL_TZ).isoformat()


def clock(text):
    return datetime.strptime(text, FMT).strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------- hashing
def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------- validation
def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_body", "Expected a JSON object.")
    return data


def text_field(data, key, *, min_len=1, max_len=200, required=True, pattern=None, strip=True):
    value = data.get(key)
    if value is None:
        if required:
            raise ApiError(400, "invalid_field", f"'{key}' is required.")
        return None
    if not isinstance(value, str):
        raise ApiError(400, "invalid_field", f"'{key}' must be text.")
    if strip:
        value = value.strip()
    if not (min_len <= len(value) <= max_len):
        raise ApiError(400, "invalid_field", f"'{key}' must be {min_len}-{max_len} characters.")
    if pattern and not re.fullmatch(pattern, value):
        raise ApiError(400, "invalid_field", f"'{key}' has an invalid format.")
    return value


STUDENT_NO = r"[A-Za-z0-9][A-Za-z0-9\-_/.]{1,31}"
PHONE = r"\+?[0-9][0-9 ()\-]{5,24}"


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Small in-memory sliding window (per process; restart clears it)."""

    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key, limit, window):
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                retry = int(window - (now - q[0])) + 1
                raise ApiError(429, "rate_limited", "Too many attempts. Please try again later.",
                               {"Retry-After": str(retry)})

    def add(self, key):
        with self._lock:
            self._hits[key].append(time.monotonic())
            if len(self._hits) > 5000:
                cutoff = time.monotonic() - 3600
                for k in [k for k, q in self._hits.items() if not q or q[-1] < cutoff]:
                    del self._hits[k]

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip():
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------- auth decorators
def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or len(header) < 20:
            raise ApiError(401, "unauthorized", "Sign in to continue.")
        with connect() as c:
            row = c.execute(
                """SELECT s.id AS sid, s.user_id, s.access_expires
                   FROM api_sessions s
                   JOIN student_accounts a ON a.user_id = s.user_id
                   JOIN users u ON u.id = s.user_id
                   WHERE s.access_hash = ? AND s.revoked = 0
                     AND u.archived = 0 AND u.role = 'student'""",
                (sha(header[7:].strip()),),
            ).fetchone()
        if row is None:
            raise ApiError(401, "unauthorized", "Sign in to continue.")
        if row["access_expires"] < now_text():
            raise ApiError(401, "token_expired", "Your session expired. Refreshing is required.")
        g.user_id, g.session_id = row["user_id"], row["sid"]
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = os.environ.get("ADMIN_API_KEY", "")
        if len(key) < 16:
            raise ApiError(503, "admin_disabled",
                           "Admin API is disabled. Set ADMIN_API_KEY (16+ characters) on the server.")
        limiter.check(f"admin-fail:{client_ip()}", 10, 600)
        provided = request.headers.get("X-Admin-Key", "")
        if not hmac.compare_digest(provided.encode(), key.encode()):
            limiter.add(f"admin-fail:{client_ip()}")
            raise ApiError(401, "unauthorized", "Invalid admin key.")
        return fn(*args, **kwargs)
    return wrapper


@bp.get("/health")
def health():
    return jsonify(status="ok", server_time=now_dt().isoformat())
