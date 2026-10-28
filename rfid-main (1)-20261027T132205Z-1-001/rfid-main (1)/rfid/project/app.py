from flask import Flask, request, jsonify
import database as db
from api import init_api, notify_after_tap, purge_user_data

app = Flask(__name__)

# Visitors may stay this many hours before the visit is flagged OVERSTAY.
VISITOR_LIMIT_HOURS = 4

# Latest card tapped, so the PC UI can capture UIDs during registration
last_tap = {"uid": None, "count": 0}
# Latest public-facing kiosk result. The kiosk reads this without exposing admin data.
latest_display_event = {
    "event_id": 0, "status": "idle", "message": "Ready to scan",
    "name": "", "role": "", "action": "", "result": "", "uid": "",
    "cooldown_seconds": 0
}

@app.route("/tap", methods=["POST"])
def tap():
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip().upper()

    if not uid:
        return jsonify({"status": "error", "message": "No UID received"})

    last_tap["uid"] = uid
    last_tap["count"] += 1

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
    name = user["name"] if user else ""
    role = user["role"] if user else ""

    if code == "TIME_IN_OK":
        status, action, message = "granted", "IN", f"Welcome, {name}!"
    elif code == "TIME_OUT_OK":
        status, action, message = "granted", "OUT", f"Goodbye, {name}!"
    elif code == "REJECT_COOLDOWN":
        status, action = "warning", ""
        message = f"Please wait {cooldown_seconds} seconds before scanning again."
    elif code == "ALREADY_CHECKED_IN":
        status, action, message = "warning", "", "Please wait before scanning again."
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
    latest_display_event.update({
        "event_id": latest_display_event["event_id"] + 1,
        **result,
        "uid": uid
    })
    return jsonify(result)


@app.route("/last-tap", methods=["GET"])
def last_tap_route():
    return jsonify(last_tap)

@app.route("/display-event", methods=["GET"])
def display_event_route():
    """Return only the latest tap result for the public-facing kiosk display."""
    return jsonify(latest_display_event)

@app.route("/users", methods=["GET"])
def users_list():
    return jsonify(db.get_all_users())

@app.route("/users", methods=["POST"])
def users_create():
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip().upper()
    name = str(data.get("name", "")).strip()
    role = str(data.get("role", "")).strip().lower()
    time_limit_minutes = data.get("time_limit_minutes")

    if not uid or not name or role not in ("student", "visitor"):
        return jsonify({"success": False, "message": "Missing or invalid fields"})

    if time_limit_minutes is not None:
        try:
            time_limit_minutes = int(time_limit_minutes)
            if time_limit_minutes < 1:
                return jsonify({"success": False, "message": "Time limit must be at least 1 minute"})
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid time limit"})

    existing = db.uid_exists(uid)
    if existing:
        if existing["is_temp"]:
            db.upgrade_temp_user(uid, name, role, time_limit_minutes)
            return jsonify({"success": True, "message": f"{name} registered ({role}) - temp card upgraded"})
        # Visitor cards are always reusable: archive the old holder's record
        # (kept in history) and give the card to the new visitor.
        if existing["role"] == "visitor":
            if db.re_register_visitor_card(uid, name, role, time_limit_minutes):
                return jsonify({"success": True, "message": f"{name} registered as {role} - card reused"})
            return jsonify({"success": False, "message": "Could not re-register this card"})
        return jsonify({"success": False, "message": "Student cards are permanent. Delete this card first to reuse it."})

    db.add_user(uid, name, role, time_limit_minutes=time_limit_minutes)
    return jsonify({"success": True, "message": f"{name} registered as {role}"})

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
