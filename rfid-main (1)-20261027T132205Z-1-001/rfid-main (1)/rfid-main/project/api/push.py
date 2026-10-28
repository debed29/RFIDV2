"""Background worker: creates inbox rows after confirmed taps and delivers Expo push messages.

Nothing here runs on the /tap request thread except a non-blocking queue append, so a slow
database, a slow Expo call or an exception can never delay or change the ESP32 response.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from .common import connect, now_text

log = logging.getLogger("api.push")

EXPO_SEND_URL = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
RETRY_DELAYS = [5, 30, 120, 600]          # seconds between delivery attempts
RECEIPT_DELAY_MIN = 15                      # Expo asks to wait ~15 min before reading receipts
RECEIPT_INTERVAL = 300

_jobs = []
_cv = threading.Condition()
_pending = 0
_thread = None
_start_lock = threading.Lock()


class Transient(Exception):
    """Network/5xx/429 problem: worth retrying later."""


def push_enabled():
    return os.environ.get("PUSH_ENABLED", "1") != "0"


def _post(url, payload):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if os.environ.get("EXPO_ACCESS_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["EXPO_ACCESS_TOKEN"]
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code >= 500:
            raise Transient(f"HTTP {e.code}")
        raise RuntimeError(f"Expo rejected request: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Transient(str(e))


def send_messages(messages):
    """Return one Expo ticket per message. Tests replace this function."""
    return _post(EXPO_SEND_URL, messages).get("data", [])


def get_receipts(ids):
    """Return {ticket_id: receipt}. Tests replace this function."""
    return _post(EXPO_RECEIPTS_URL, {"ids": ids}).get("data", {})


# ------------------------------------------------------------------ queue
def submit(kind, delay=0.0, attempt=0, **payload):
    ensure_started()
    global _pending
    with _cv:
        _jobs.append({"run_at": time.time() + delay, "kind": kind, "attempt": attempt, **payload})
        _pending += 1
        _cv.notify_all()


def drain(timeout=10.0):
    """Block until every queued job (including delayed retries) has run. Used by tests."""
    end = time.time() + timeout
    with _cv:
        while _pending > 0:
            left = end - time.time()
            if left <= 0:
                return False
            _cv.wait(left)
    return True


def ensure_started():
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="api-push-worker", daemon=True)
        _thread.start()
    _requeue_pending()


def _requeue_pending():
    """After a restart, retry pushes that were created but never delivered (last 2 hours)."""
    try:
        from .common import later
        with connect() as c:
            rows = c.execute("SELECT id FROM notifications WHERE push_status = 'pending' AND created_at >= ?",
                             (later(hours=-2),)).fetchall()
        for r in rows:
            submit("push", nid=r["id"])
    except Exception:
        log.exception("could not requeue pending pushes")


def _loop():
    global _pending
    next_receipts = time.time() + RECEIPT_INTERVAL
    while True:
        job = None
        with _cv:
            now = time.time()
            due = [j for j in _jobs if j["run_at"] <= now]
            if due:
                job = min(due, key=lambda j: j["run_at"])
                _jobs.remove(job)
            else:
                nxt = min([j["run_at"] for j in _jobs] + [next_receipts])
                _cv.wait(timeout=max(0.05, min(nxt - now, 30)))
        if job:
            try:
                _handle(job)
            except Exception:
                log.exception("push job %s failed", job.get("kind"))
            finally:
                with _cv:
                    _pending -= 1
                    _cv.notify_all()
        if time.time() >= next_receipts:
            next_receipts = time.time() + RECEIPT_INTERVAL
            try:
                check_receipts()
            except Exception:
                log.exception("receipt check failed")


def _handle(job):
    from . import notifier
    if job["kind"] == "tap":
        nid = notifier.process_tap(job["code"], job["user_id"], job.get("log_id"))
        if nid:
            _deliver(nid, None, 0)
    elif job["kind"] == "push":
        _deliver(job["nid"], job.get("tokens"), job["attempt"])


# ------------------------------------------------------------------ delivery
def _mark(nid, status, bump=False):
    with connect(write=True) as c:
        c.execute("UPDATE notifications SET push_status = ?, push_attempts = push_attempts + ? WHERE id = ?",
                  (status, 1 if bump else 0, nid))


def _retry(nid, tokens, attempt, reason):
    if attempt >= len(RETRY_DELAYS):
        log.warning("giving up on notification %s: %s", nid, reason)
        _mark(nid, "failed", bump=True)
        return
    with connect(write=True) as c:
        c.execute("UPDATE notifications SET push_attempts = push_attempts + 1 WHERE id = ?", (nid,))
    submit("push", delay=RETRY_DELAYS[attempt], attempt=attempt + 1, nid=nid, tokens=tokens)


def _deliver(nid, only_tokens, attempt):
    with connect() as c:
        n = c.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone()
        if n is None or n["push_status"] in ("sent", "skipped", "failed"):
            return
        prefs = c.execute("SELECT * FROM notification_prefs WHERE user_id = ?", (n["user_id"],)).fetchone()
        devices = [r["token"] for r in c.execute(
            "SELECT token FROM push_devices WHERE user_id = ? AND disabled_at IS NULL", (n["user_id"],))]

    def pref(key):
        return True if prefs is None else bool(prefs[key])

    allowed = {"attendance": pref("attendance_alerts"), "system": pref("announcements"),
               "emergency": True}[n["category"]]  # safety notices ignore optional preferences
    tokens = [t for t in devices if only_tokens is None or t in only_tokens]
    if not push_enabled() or not allowed or not tokens:
        _mark(nid, "skipped")
        return

    messages = []
    for tok in tokens:
        m = {"to": tok, "title": n["title"], "body": n["body"], "channelId": n["category"],
             "data": {"notificationId": nid, "category": n["category"]},
             "priority": "high" if n["category"] != "system" else "default",
             "ttl": 3600 if n["category"] == "attendance" else 86400}
        if pref("sound"):
            m["sound"] = "default"
        messages.append(m)

    try:
        tickets = send_messages(messages)
    except Transient as e:
        _retry(nid, tokens, attempt, str(e))
        return
    except Exception:
        log.exception("push send failed permanently for notification %s", nid)
        _mark(nid, "failed", bump=True)
        return

    ok, retry_tokens = 0, []
    with connect(write=True) as c:
        for tok, t in zip(tokens, tickets):
            if t.get("status") == "ok":
                ok += 1
                if t.get("id"):
                    c.execute("INSERT OR IGNORE INTO push_tickets (ticket_id, token, notification_id, created_at)"
                              " VALUES (?, ?, ?, ?)", (t["id"], tok, nid, now_text()))
                continue
            err = (t.get("details") or {}).get("error")
            if err == "DeviceNotRegistered":
                c.execute("UPDATE push_devices SET disabled_at = ? WHERE token = ?", (now_text(), tok))
            elif err == "MessageRateExceeded":
                retry_tokens.append(tok)
            else:
                log.warning("push ticket error for notification %s: %s", nid, t.get("message") or err)
    if retry_tokens:
        _retry(nid, retry_tokens, attempt, "MessageRateExceeded")
    else:
        _mark(nid, "sent" if ok else "failed", bump=True)


def check_receipts():
    """Second chance to catch DeviceNotRegistered, which Expo reports in receipts."""
    from .common import later
    with connect() as c:
        rows = c.execute("SELECT ticket_id, token FROM push_tickets WHERE checked_at IS NULL AND created_at <= ?"
                         " LIMIT 500", (later(minutes=-RECEIPT_DELAY_MIN),)).fetchall()
    if not rows:
        return
    receipts = get_receipts([r["ticket_id"] for r in rows])
    with connect(write=True) as c:
        for r in rows:
            rec = receipts.get(r["ticket_id"])
            if rec is None:
                continue
            if rec.get("status") == "error" and (rec.get("details") or {}).get("error") == "DeviceNotRegistered":
                c.execute("UPDATE push_devices SET disabled_at = ? WHERE token = ?", (now_text(), r["token"]))
            c.execute("UPDATE push_tickets SET checked_at = ? WHERE ticket_id = ?", (now_text(), r["ticket_id"]))
        c.execute("DELETE FROM push_tickets WHERE created_at < ?", (later(days=-2),))
