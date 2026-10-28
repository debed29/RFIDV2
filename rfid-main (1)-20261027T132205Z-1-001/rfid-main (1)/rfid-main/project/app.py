from flask import Flask, request, jsonify
import sqlite3
import threading

import database as db
from api import init_api, notify_after_tap, purge_user_data

app = Flask(__name__)

# Visitors may stay this many hours before the visit is flagged OVERSTAY.
VISITOR_LIMIT_HOURS = 4

# Latest card tapped, so the PC UI can capture UIDs during registration
last_tap = {"uid": None, "count": 0}
capture_lock = threading.Lock()
capture_session_active = False
captured_uids = set()
# Latest public-facing kiosk result. The kiosk reads this without exposing admin data.
latest_display_event = {
    "event_id": 0, "status": "idle", "message": "Ready to scan",
    "name": "", "role": "", "action": "", "result": "", "uid": "",
    "cooldown_seconds": 0
}
latest_registration_event = {
    "event_id": 0, "status": "idle", "name": "", "role": "", "uid": ""
}

@app.route("/tap", methods=["POST"])
def tap():
    global capture_session_active
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip().upper()

    if not uid or len(uid) > 40:
        with capture_lock:
            if capture_session_active:
                capture_session_active = False
                message = "INVALID CARD UID"
                last_tap.update(
                    uid=uid, count=last_tap["count"] + 1,
                    capture_status="capture_failed", capture_message=message,
                )
                failure = {
                    "success": False, "status": "capture_failed",
                    "result": "INVALID_UID", "uid": uid, "message": message,
                    "name": "", "role": "", "action": "",
                }
                latest_display_event.update({
                    "event_id": latest_display_event["event_id"] + 1,
                    **failure,
                })
                return jsonify(failure)
        if not uid:
            return jsonify({"status": "error", "message": "No UID received"})
        return jsonify({"status": "error", "message": "Card UID is too long"})

    with capture_lock:
        last_tap["uid"] = uid
        last_tap["count"] += 1
        is_capture = capture_session_active
        if is_capture:
            capture_session_active = False
            registered = db.uid_exists(uid)
            if registered:
                capture_result = {
                    "success": False,
                    "status": "card_already_registered",
                    "result": "CARD_ALREADY_REGISTERED",
                    "uid": uid,
                    "message": "CARD ALREADY REGISTERED",
                    "name": "",
                    "role": "",
                    "action": "",
                }
            else:
                capture_result = {
                    "success": True,
                    "status": "capture_success",
                    "result": "CAPTURE_SUCCESS",
                    "uid": uid,
                    "message": "CARD READ SUCCESSFULLY",
                    "name": "",
                    "role": "",
                    "action": "",
                }
                captured_uids.add(uid)
            last_tap.update(
                capture_status=capture_result["status"],
                capture_message=capture_result["message"],
            )
            latest_display_event.update({
                "event_id": latest_display_event["event_id"] + 1,
                **capture_result,
            })
            return jsonify(capture_result)

    # Pass per-card time limit if available; otherwise fall back to global limit
    card_limit = db.get_visitor_time_limit_minutes(uid)
    if card_limit is not None:
        limit_hours = card_limit / 60.0
    else:
        limit_hours = VISITOR_LIMIT_HOURS
    code, user, log_id, cooldown_seconds = db.record_tap(uid, limit_hours)
    # Queued after the committed tap; never blocks or raises.  ``log_id``
    # identifies the exact record rather than relying on the newest log.
    notify_after_tap(code, user, log_id)
    name = db.format_user_name(user) if user else ""
    role = user["role"] if user else ""

    if code == "TIME_IN_OK":
        status, action, message = "granted", "IN", f"Welcome, {name}!"
    elif code == "TIME_OUT_OK":
        status, action, message = "granted", "OUT", f"Goodbye, {name}!"
    elif code == "REJECT_COOLDOWN":
        status, action = "warning", ""
        message = f"Please wait {cooldown_seconds} seconds before scanning again."
    elif code == "ALREADY_CHECKED_IN":
        status, action, message = "warning", "", "Already checked in. Please wait before scanning again."
    elif code == "VISIT_LIMIT_EXCEEDED":
        status, action, message = "denied", "", "Visit limit exceeded. Please see staff."
    elif code == "REJECT_NO_OPEN_RECORD":
        status, action, message = "denied", "", "No time in on record. Please see staff."
    elif code == "REJECT_EXPIRED_VISITOR":
        status, action, message = "denied", "", "Visitor card already used. Please register it for a new visitor."
        name, role = "", ""
    elif code == "OVERSTAY":
        status, action, message = "warning", "OUT", "Visitor card has overstayed. Please register a new card."
    else:
        status, action, message = "denied", "", "Card not registered. Please see staff."
        name, role = "", ""

    result = {
        "status": status, "result": code, "name": name, "role": role,
        "action": action, "message": message
    }
    if cooldown_seconds is not None:
        result["cooldown_seconds"] = cooldown_seconds
    with capture_lock:
        latest_display_event.update({
            "event_id": latest_display_event["event_id"] + 1,
            **result,
            "uid": uid
        })
        last_tap.update(capture_status="", capture_message="")
    return jsonify(result)


@app.route("/capture/start", methods=["POST"])
def capture_start():
    global capture_session_active
    with capture_lock:
        if capture_session_active:
            return jsonify({
                "success": False,
                "status": "capture_in_progress",
                "message": "A card capture is already in progress.",
                **last_tap,
            })
        capture_session_active = True
        last_tap.update(capture_status="waiting", capture_message="PLEASE TAP YOUR RFID CARD")
        return jsonify({
            "success": True,
            "status": "capture_waiting",
            "message": "PLEASE TAP YOUR RFID CARD",
            **last_tap,
        })


@app.route("/capture/cancel", methods=["POST"])
def capture_cancel():
    global capture_session_active
    with capture_lock:
        capture_session_active = False
        last_tap.update(capture_status="", capture_message="")
    return jsonify({"success": True, "status": "capture_cancelled"})


@app.route("/last-tap", methods=["GET"])
def last_tap_route():
    return jsonify(last_tap)

@app.route("/display-event", methods=["GET"])
def display_event_route():
    """Return only the latest tap result for the public-facing kiosk display."""
    return jsonify(latest_display_event)

@app.route("/registration-event", methods=["GET"])
def registration_event_route():
    """Return the most recent successful card registration for the LCD reader."""
    return jsonify(latest_registration_event)

@app.route("/users", methods=["GET"])
def users_list():
    return jsonify(db.get_all_users())

@app.route("/users", methods=["POST"])
def users_create():
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip().upper()
    role = str(data.get("role", "")).strip().lower()
    surname = str(data.get("surname", "")).strip()
    first_name = str(data.get("first_name", "")).strip()
    middle_name = str(data.get("middle_name", "")).strip()
    student_number = str(data.get("student_number") or "").strip()
    if surname or first_name or middle_name:
        if not surname or not first_name:
            return jsonify({"success": False, "message": "Surname and first name are required"})
        name = " ".join(part for part in (surname, first_name, middle_name) if part)
    else:
        name = str(data.get("name", "")).strip()
    time_limit_minutes = data.get("time_limit_minutes")

    if not uid or not name or role not in ("student", "visitor"):
        return jsonify({"success": False, "message": "Missing or invalid fields"})
    if len(uid) > 40 or len(name) > 160:
        return jsonify({"success": False, "message": "Card UID or name is too long"})
    if any(len(part) > 80 for part in (surname, first_name, middle_name)):
        return jsonify({"success": False, "message": "Name fields must be 80 characters or fewer"})
    if role == "student" and not student_number:
        return jsonify({"success": False, "message": "Student number is required for students"})
    if len(student_number) > 32:
        return jsonify({"success": False, "message": "Student number must be 32 characters or fewer"})

    if time_limit_minutes is not None:
        try:
            time_limit_minutes = int(time_limit_minutes)
            if time_limit_minutes < 1:
                return jsonify({"success": False, "message": "Time limit must be at least 1 minute"})
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid time limit"})

    existing = db.uid_exists(uid)
    if existing:
        return jsonify({
            "success": False,
            "status": "card_already_registered",
            "message": "CARD ALREADY REGISTERED",
        })
    if student_number and db.student_number_exists(
        student_number
    ):
        return jsonify({"success": False, "message": "That student number is already registered"})
    with capture_lock:
        if uid not in captured_uids:
            return jsonify({
                "success": False,
                "status": "capture_required",
                "message": "Capture this card UID before registering it.",
            })

    try:
        student_number = db.add_user(
            uid, name, role, time_limit_minutes=time_limit_minutes,
            surname=surname or None, first_name=first_name or None, middle_name=middle_name or None,
            student_number=student_number or None,
        )
    except sqlite3.IntegrityError:
        if student_number and db.student_number_exists(student_number):
            return jsonify({"success": False, "message": "That student number is already registered"})
        if db.uid_exists(uid):
            return jsonify({
                "success": False,
                "status": "card_already_registered",
                "message": "CARD ALREADY REGISTERED",
            })
        raise
    with capture_lock:
        captured_uids.discard(uid)
    message = f"{name} registered as {role}"
    if student_number:
        message += f" - Student number: {student_number}"
    with capture_lock:
        latest_registration_event.update(
            event_id=latest_registration_event["event_id"] + 1,
            status="registration_success",
            name=name,
            role=role,
            uid=uid,
        )
        latest_display_event.update({
            "event_id": latest_display_event["event_id"] + 1,
            "status": "registration_success",
            "message": "Successfully registered",
            "name": name,
            "role": role,
            "action": "",
            "result": "REGISTRATION_SUCCESS",
            "uid": uid,
            "cooldown_seconds": 0,
        })
    return jsonify({"success": True, "student_number": student_number, "message": message})


@app.route("/users/<int:user_id>", methods=["PATCH"])
def users_update(user_id):
    data = request.get_json(silent=True) or {}
    surname = str(data.get("surname", "")).strip()
    first_name = str(data.get("first_name", "")).strip()
    middle_name = str(data.get("middle_name", "")).strip()
    if not surname or not first_name:
        return jsonify({"success": False, "message": "Surname and first name are required"})
    if len(surname) > 80 or len(first_name) > 80 or len(middle_name) > 80:
        return jsonify({"success": False, "message": "Name fields must be 80 characters or fewer"})
    if not db.update_user_name(user_id, surname, first_name, middle_name):
        return jsonify({"success": False, "message": "Student not found"})
    return jsonify({"success": True, "message": "Student name updated"})

@app.route("/users/<uid>", methods=["DELETE"])
def users_delete(uid):
    existing = db.uid_exists(uid.upper())
    db.delete_user(uid.upper())
    if existing:
        purge_user_data(existing["id"])  # remove mobile accounts/tokens/notifications for this user
    return jsonify({"success": True})

@app.route("/logs", methods=["GET"])
def logs_route():
    return jsonify(db.get_recent_logs())


@app.route("/today-count", methods=["GET"])
def today_count_route():
    return jsonify({"count": db.count_today()})


init_api(app)  # registers /api/v1 and creates the additive mobile tables


if __name__ == "__main__":
    db.init_db()
    print("Server running! Keep this window open.")
    app.run(host="0.0.0.0", port=5001)
