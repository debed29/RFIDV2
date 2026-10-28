"""Attendance API tests and an opt-in live server check.

Run the isolated tests with ``python -m unittest test_server``. A live server
check reads public endpoints by default. Use ``--tap-uid UID`` only when you
intend to write a real attendance record.
"""
import argparse
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from unittest.mock import patch

import app as attendance_app
import database as db
from api import push
from api.schema import init_api_schema


def post(server, path, payload):
    request = urllib.request.Request(
        server.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def get(server, path):
    with urllib.request.urlopen(server.rstrip("/") + path, timeout=5) as response:
        return response.status, json.loads(response.read())


class AttendanceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_name = db.DB_NAME
        db.DB_NAME = os.path.join(self.temp_dir.name, "test.db")
        db.init_db()
        # app.py normally creates the mobile tables at startup.  This suite
        # swaps in an isolated SQLite file, so initialize the same additive
        # schema before exercising routes that enqueue notifications.
        init_api_schema()
        attendance_app.app.config["TESTING"] = True
        attendance_app.last_tap.update(uid=None, count=0)
        attendance_app.latest_display_event.update(
            event_id=0, status="idle", message="Ready to scan", name="",
            role="", action="", result="", uid=""
        )
        self.client = attendance_app.app.test_client()

    def tearDown(self):
        # /tap queues post-commit notification work.  Wait while DB_NAME still
        # points to this test's temporary SQLite file, otherwise Windows can
        # reject TemporaryDirectory cleanup because the worker has it open.
        try:
            self.assertTrue(push.drain(timeout=5), "notification worker did not finish")
        finally:
            db.DB_NAME = self.previous_db_name
            self.temp_dir.cleanup()

    def add_user(self, uid="A1B2C3D4", name="Test Student", role="student"):
        response = self.client.post("/users", json={"uid": uid, "name": name, "role": role})
        self.assertTrue(response.json["success"])

    def test_normal_tap_is_rejected_during_cooldown(self):
        from unittest.mock import patch
        self.add_user()

        with patch("database.unix_time", side_effect=[1000, 1010]):
            first = self.client.post("/tap", json={"uid": "A1B2C3D4"}).json
            repeated = self.client.post("/tap", json={"uid": "A1B2C3D4"}).json

        self.assertEqual((first["status"], first["action"]), ("granted", "IN"))
        self.assertEqual(repeated["result"], "REJECT_COOLDOWN")
        self.assertEqual(repeated["status"], "warning")

    def test_taps_automatically_toggle_attendance_after_cooldown(self):
        from unittest.mock import patch
        self.add_user()
        with patch("database.unix_time", side_effect=[1000, 1030, 1060]):
            timed_in = self.client.post("/tap", json={"uid": "A1B2C3D4"}).json
            timed_out = self.client.post("/tap", json={"uid": "A1B2C3D4"}).json
            next_tap = self.client.post("/tap", json={"uid": "A1B2C3D4"}).json
        self.assertEqual((timed_in["status"], timed_in["action"]), ("granted", "IN"))
        self.assertEqual((timed_out["status"], timed_out["action"]), ("granted", "OUT"))
        self.assertEqual((next_tap["status"], next_tap["action"]), ("granted", "IN"))
        self.assertEqual(self.client.post("/timeout-arm").status_code, 404)

    def test_unknown_card_is_denied_without_creating_user(self):
        result = self.client.post("/tap", json={"uid": "A1B2C3D4"}).json

        self.assertEqual(result["result"], "DENY_UNKNOWN")
        self.assertEqual(db.get_all_users(), [])

    def test_existing_temporary_card_can_be_upgraded(self):
        db.add_user("A1B2C3D4", "Temp Visitor", "visitor", is_temp=1)

        response = self.client.post("/users", json={
            "uid": "A1B2C3D4", "name": "Registered Visitor", "role": "visitor"
        })

        self.assertTrue(response.json["success"])
        user = db.uid_exists("A1B2C3D4")
        self.assertEqual(user["name"], "Registered Visitor")
        self.assertEqual(user["is_temp"], 0)

    def test_overstay_is_flagged_but_can_be_checked_out(self):
        from unittest.mock import patch
        self.add_user(uid="V1", name="Test Visitor", role="visitor")
        start = datetime(2026, 10, 12, 8, 0, 0, tzinfo=db.SCHOOL_TZ)
        late = start + timedelta(hours=4, seconds=1)
        with patch.object(db, "school_now", side_effect=[start, late]), \
                patch("database.unix_time", side_effect=[2000, 2060]):
            self.client.post("/tap", json={"uid": "V1"})
            overstay = self.client.post("/tap", json={"uid": "V1"}).json

        self.assertEqual((overstay["status"], overstay["action"]), ("granted", "OUT"))
        self.assertEqual(db.get_recent_logs()[0]["flag"], "OVERSTAY")

    def test_first_tap_after_limit_can_time_out_and_still_flag_overstay(self):
        from unittest.mock import patch
        self.add_user(uid="V1", name="Test Visitor", role="visitor")
        start = datetime(2026, 10, 12, 8, 0, 0, tzinfo=db.SCHOOL_TZ)
        with patch.object(db, "school_now", return_value=start):
            self.client.post("/tap", json={"uid": "V1"})
        with patch.object(db, "school_now", return_value=start + timedelta(hours=4, seconds=1)), \
                patch("database.unix_time", return_value=db.uid_exists("V1")["last_successful_tap"] + 60):
            checkout = self.client.post("/tap", json={"uid": "V1"}).json

        self.assertEqual(checkout["result"], "TIME_OUT_OK")
        self.assertEqual(db.get_recent_logs()[0]["flag"], "OVERSTAY")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check a running RFID attendance server")
    parser.add_argument(
        "--server", default=os.environ.get("RFID_SERVER_URL", "http://127.0.0.1:5001"),
        help="Base URL for the Flask server (default: %(default)s)",
    )
    parser.add_argument(
        "--tap-uid", metavar="UID",
        help="Send a real tap request, which changes attendance data",
    )
    args = parser.parse_args()

    print(f"Target: {args.server}")
    try:
        for endpoint in ("/display-event", "/today-count", "/users"):
            status, body = get(args.server, endpoint)
            suffix = f" ({len(body)} records)" if endpoint == "/users" else ""
            print(f"  GET {endpoint:<15} -> {status}{suffix}")
        if args.tap_uid:
            status, body = post(args.server, "/tap", {"uid": args.tap_uid})
            print(f"  POST /tap          -> {status} {body}")
    except urllib.error.HTTPError as error:
        print(f"HTTP error {error.code}: server reachable, request rejected")
        raise SystemExit(1)
    except (urllib.error.URLError, OSError) as error:
        print(f"Server check failed: {error}")
        raise SystemExit(1)
