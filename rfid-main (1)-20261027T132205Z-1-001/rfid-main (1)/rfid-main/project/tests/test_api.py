import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ADMIN_API_KEY"] = "test-admin-key-0123456789"

import database as db  # noqa: E402

_tmp = tempfile.mkdtemp()
db.DB_NAME = os.path.join(_tmp, "test.db")

import app as flask_app  # noqa: E402
from api import push  # noqa: E402
from api.common import limiter  # noqa: E402

push.RETRY_DELAYS = [0.05, 0.05]
ADMIN = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
TOKEN = "ExponentPushToken[abcdefghijklmnopqrstuv]"
TOKEN2 = "ExponentPushToken[zzzzzzzzzzzzzzzzzzzzzz]"


class Base(unittest.TestCase):
    def setUp(self):
        with closing(db.get_conn()) as c:
            for t in ("users", "logs", "events", "student_profiles", "student_accounts", "claim_codes",
                      "api_sessions", "notifications", "notification_prefs", "push_devices",
                      "push_tickets", "faculty_contacts"):
                c.execute(f"DELETE FROM {t}")
        limiter.reset()
        push.drain()
        flask_app.capture_session_active = False
        flask_app.captured_uids.clear()
        flask_app.last_tap.update(uid=None, count=0, capture_status="", capture_message="")
        flask_app.latest_display_event.update(
            event_id=0, status="idle", message="Ready to scan", name="",
            role="", action="", result="", uid=""
        )
        flask_app.latest_registration_event.update(
            event_id=0, status="idle", name="", role="", uid=""
        )
        self.sent = []
        self.fail_next = []
        push.send_messages = self._fake_send
        self.cl = flask_app.app.test_client()
        self.n = 0

    def _fake_send(self, messages):
        if self.fail_next:
            exc = self.fail_next.pop(0)
            if exc:
                raise exc
        self.sent.extend(messages)
        return [{"status": "ok", "id": f"t{len(self.sent)}-{i}"} for i, _ in enumerate(messages)]

    # helpers
    def admin(self, method, path, **kw):
        return getattr(self.cl, method)("/api/v1" + path, headers=ADMIN, **kw)

    def auth(self, method, path, token, **kw):
        return getattr(self.cl, method)("/api/v1" + path, headers={"Authorization": "Bearer " + token}, **kw)

    def tap(self, uid):
        return self.cl.post("/tap", json={"uid": uid})

    def capture_card(self, uid):
        started = self.cl.post("/capture/start")
        self.assertTrue(started.json["success"], started.json)
        captured = self.tap(uid)
        self.assertEqual(captured.json["status"], "capture_success", captured.json)
        return captured

    def register_card(self, payload):
        self.capture_card(payload["uid"])
        return self.cl.post("/users", json=payload)

    def student(self, uid="AA11", name="Juan", sn="2023-0142", claim=True, pw="correct horse"):
        self.assertTrue(self.register_card({
            "uid": uid, "name": name, "role": "student", "student_number": f"RFID-{uid}",
        }).json["success"])
        user_id = db.uid_exists(uid)["id"]
        r = self.admin("put", f"/admin/students/{user_id}/profile",
                       json={"student_number": sn, "course_section": "BSIT - 2A", "phone": "+63 912 345 6789"})
        self.assertEqual(r.status_code, 200, r.json)
        out = {"user_id": user_id, "uid": uid, "sn": sn, "pw": pw}
        if claim:
            code = self.admin("post", f"/admin/students/{user_id}/claim-code").json["claim_code"]
            r = self.cl.post("/api/v1/auth/claim", json={"student_number": sn, "claim_code": code, "password": pw})
            self.assertEqual(r.status_code, 201, r.json)
            out.update(code=code, access=r.json["access_token"], refresh=r.json["refresh_token"])
        return out

    def register_device(self, s, token=TOKEN):
        r = self.auth("post", "/devices", s["access"], json={"token": token, "platform": "android"})
        self.assertEqual(r.status_code, 200, r.json)

    def settle(self):
        self.assertTrue(push.drain(10))


class TapContract(Base):
    def test_registration_success_is_sent_to_the_student_display_and_lcd(self):
        created = self.register_card({
            "uid": "REGISTERED1",
            "role": "student",
            "surname": "Doe",
            "first_name": "Jane",
            "student_number": "REG-001",
        })

        self.assertTrue(created.json["success"], created.json)
        event = self.cl.get("/registration-event").json
        self.assertEqual(
            (event["event_id"], event["status"], event["name"], event["role"], event["uid"]),
            (1, "registration_success", "Doe Jane", "student", "REGISTERED1"),
        )
        display_event = self.cl.get("/display-event").json
        self.assertEqual(display_event["status"], "registration_success")
        self.assertEqual(display_event["message"], "Successfully registered")

    def test_capture_is_single_use_and_does_not_create_attendance(self):
        started = self.cl.post("/capture/start")
        self.assertEqual(started.json["message"], "PLEASE TAP YOUR RFID CARD")

        captured = self.tap("CAPTURE1").json

        self.assertEqual(
            (captured["success"], captured["status"], captured["message"], captured["uid"]),
            (True, "capture_success", "CARD READ SUCCESSFULLY", "CAPTURE1"),
        )
        self.assertFalse(flask_app.capture_session_active)
        self.assertEqual(flask_app.latest_display_event["status"], "capture_success")
        self.assertEqual(db.get_recent_logs(), [])

        next_scan = self.tap("CAPTURE1").json
        self.assertEqual(next_scan["status"], "denied")
        self.assertEqual(db.get_recent_logs(), [])

    def test_capture_duplicate_uid_is_rejected_and_does_not_create_attendance(self):
        self.student("ALREADY1", claim=False)
        self.cl.post("/capture/start")

        result = self.tap("ALREADY1").json

        self.assertEqual(
            (result["success"], result["status"], result["message"]),
            (False, "card_already_registered", "CARD ALREADY REGISTERED"),
        )
        self.assertEqual(flask_app.latest_display_event["status"], "card_already_registered")
        self.assertEqual(len(db.get_recent_logs()), 0)

    def test_capture_session_rejects_overlapping_start_requests(self):
        first = self.cl.post("/capture/start").json
        second = self.cl.post("/capture/start").json

        self.assertEqual(first["status"], "capture_waiting")
        self.assertFalse(second["success"])
        self.assertEqual(second["status"], "capture_in_progress")
        self.cl.post("/capture/cancel")

    def test_invalid_uid_ends_capture_without_success(self):
        self.cl.post("/capture/start")

        result = self.tap("X" * 41).json

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "capture_failed")
        self.assertEqual(result["message"], "INVALID CARD UID")
        self.assertFalse(flask_app.capture_session_active)
        self.assertEqual(db.get_recent_logs(), [])

    def test_registration_requires_successful_capture(self):
        result = self.cl.post("/users", json={
            "uid": "NO-CAPTURE", "role": "student", "surname": "No",
            "first_name": "Capture", "student_number": "NC-001",
        }).json

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "capture_required")
        self.assertIsNone(db.uid_exists("NO-CAPTURE"))

    def test_deleted_card_can_be_reregistered_without_losing_attendance_history(self):
        self.register_card({
            "uid": "ARCHIVE1", "role": "student", "surname": "Doe", "first_name": "Jane",
            "student_number": "ARCHIVE-SN-1",
        })
        user = db.uid_exists("ARCHIVE1")
        with closing(db.get_conn()) as conn:
            conn.execute(
                "INSERT INTO logs (user_id, time_in) VALUES (?, '2026-10-28 08:00:00')",
                (user["id"],),
            )

        deleted = self.cl.delete("/users/ARCHIVE1")
        self.assertTrue(deleted.json["success"])
        self.assertTrue(db.uid_exists("ARCHIVE1", include_archived=True)["archived"])
        self.assertEqual(len(db.get_recent_logs()), 1)
        self.assertEqual(self.tap("ARCHIVE1").json["status"], "denied")

        self.capture_card("ARCHIVE1")
        re_registered = self.cl.post("/users", json={
            "uid": "ARCHIVE1", "role": "student", "surname": "Roe", "first_name": "John",
            "student_number": "ARCHIVE-SN-1",
        }).json

        self.assertTrue(re_registered["success"], re_registered)
        active = db.uid_exists("ARCHIVE1")
        self.assertFalse(active["archived"])
        self.assertNotEqual(active["id"], user["id"])
        self.assertEqual(len(db.get_recent_logs()), 1)
        with closing(db.get_conn()) as conn:
            old_log = conn.execute(
                "SELECT user_id FROM logs WHERE user_id = ?", (user["id"],)
            ).fetchone()
        self.assertEqual(old_log["user_id"], user["id"])

    def test_tap_response_shape_unchanged(self):
        r = self.tap("NOPE")
        self.assertEqual(r.json, {"status": "denied", "result": "DENY_UNKNOWN", "name": "", "role": "",
                                  "action": "", "message": "Card not registered. Please see staff."})
        self.student("AA11", claim=False)
        from unittest import mock
        with mock.patch("database.unix_time", side_effect=[1000.0, 1029.2, 1030.0, 1060.0]):
            first = self.tap("aa11")
            duplicate = self.tap("AA11")
            timed_out = self.tap("AA11")
            checked_in_again = self.tap("AA11")

        self.assertEqual(set(first.json), {"status", "result", "name", "role", "action", "message"})
        self.assertEqual((first.json["status"], first.json["result"], first.json["action"]),
                         ("granted", "TIME_IN_OK", "IN"))
        self.assertEqual((duplicate.json["result"], duplicate.json["cooldown_seconds"]),
                         ("REJECT_COOLDOWN", 1))
        self.assertEqual((timed_out.json["result"], timed_out.json["action"]), ("TIME_OUT_OK", "OUT"))
        self.assertEqual((checked_in_again.json["result"], checked_in_again.json["action"]),
                         ("TIME_IN_OK", "IN"))
        self.assertEqual(self.cl.post("/timeout-arm").status_code, 404)

    def test_registration_saves_admin_entered_number_and_edits_names_in_place(self):
        created = self.register_card({
            "uid": "STUDENT1", "role": "student", "name": "Juan Dela Cruz Santos", "surname": "Santos",
            "first_name": "Juan", "middle_name": "Dela Cruz", "student_number": "2023-00123",
        })
        self.assertEqual(created.status_code, 200, created.json)
        student_number = created.json["student_number"]
        self.assertEqual(student_number, "2023-00123")
        user = db.uid_exists("STUDENT1")
        self.assertEqual((user["name"], user["surname"], user["first_name"], user["middle_name"]),
                         ("Santos Juan Dela Cruz", "Santos", "Juan", "Dela Cruz"))
        exported_users = self.cl.get("/users").json
        exported_student = next(row for row in exported_users if row["rfid_uid"] == "STUDENT1")
        self.assertEqual(
            (exported_student["surname"], exported_student["first_name"],
             exported_student["middle_name"], exported_student["name"]),
            ("Santos", "Juan", "Dela Cruz", "Santos Juan Dela Cruz"),
        )

        duplicate = self.cl.post("/users", json={
            "uid": "STUDENT1", "role": "student", "surname": "Other", "first_name": "Person",
            "student_number": "2023-00999",
        })
        self.assertFalse(duplicate.json["success"])
        self.assertEqual(duplicate.json["message"], "CARD ALREADY REGISTERED")

        with closing(db.get_conn()) as c:
            c.execute("INSERT INTO logs (user_id, time_in) VALUES (?, '2026-10-28 08:00:00')", (user["id"],))
            log_id = c.execute("SELECT id FROM logs WHERE user_id = ?", (user["id"],)).fetchone()["id"]
        edited = self.cl.patch(f"/users/{user['id']}", json={
            "surname": "Santos", "first_name": "John", "middle_name": "Dela Cruz",
        })
        self.assertEqual(edited.status_code, 200, edited.json)
        updated = db.uid_exists("STUDENT1")
        self.assertEqual(updated["name"], "Santos John Dela Cruz")
        self.assertEqual((updated["student_number"], updated["rfid_uid"]), (student_number, "STUDENT1"))
        with closing(db.get_conn()) as c:
            log = c.execute("SELECT id, time_in FROM logs WHERE user_id = ?", (user["id"],)).fetchone()
        self.assertEqual((log["id"], log["time_in"]), (log_id, "2026-10-28 08:00:00"))

    def test_registered_users_include_name_parts_for_legacy_name_only_rows(self):
        created = self.register_card({
            "uid": "LEGACY1", "role": "student", "name": "Jane Marie Doe",
            "student_number": "LEGACY-001",
        })
        self.assertTrue(created.json["success"])
        legacy_user = db.uid_exists("LEGACY1")
        with closing(db.get_conn()) as conn:
            conn.execute(
                "UPDATE users SET surname = NULL, first_name = NULL, middle_name = NULL WHERE id = ?",
                (legacy_user["id"],),
            )

        exported_student = next(
            row for row in self.cl.get("/users").json if row["rfid_uid"] == "LEGACY1"
        )
        self.assertEqual(
            (exported_student["surname"], exported_student["first_name"],
             exported_student["middle_name"], exported_student["name"]),
            ("Doe", "Jane", "Marie", "Doe Jane Marie"),
        )

    def test_tap_response_displays_student_surname_first(self):
        created = self.register_card({
            "uid": "DISPLAY1", "role": "student", "name": "Jane Marie Doe",
            "surname": "Doe", "first_name": "Jane", "middle_name": "Marie",
            "student_number": "2023-00501",
        })
        self.assertTrue(created.json["success"])

        result = self.tap("DISPLAY1").json

        self.assertEqual(result["name"], "Doe Jane Marie")
        self.assertEqual(result["message"], "Welcome, Doe Jane Marie!")

    def test_tap_response_formats_legacy_student_name_surname_first(self):
        db.add_user("LEGACYDISPLAY", "Jane Marie Doe", "student")

        result = self.tap("LEGACYDISPLAY").json

        self.assertEqual(result["name"], "Doe Jane Marie")
        self.assertEqual(result["message"], "Welcome, Doe Jane Marie!")

    def test_visitor_split_names_are_surname_first_in_taps_and_recent_logs(self):
        created = self.register_card({
            "uid": "VISITOR1", "role": "visitor", "surname": "Dela Cruz",
            "first_name": "Ana", "middle_name": "Marie", "student_number": "VISITOR-001",
        })
        self.assertTrue(created.json["success"], created.json)
        self.assertEqual(db.uid_exists("VISITOR1")["student_number"], "VISITOR-001")

        result = self.tap("VISITOR1").json

        self.assertEqual(result["name"], "Dela Cruz Ana Marie")
        self.assertEqual(db.get_recent_logs()[0]["name"], "Dela Cruz Ana Marie")

    def test_student_registration_requires_number_and_rejects_duplicate_number(self):
        missing = self.cl.post("/users", json={
            "uid": "MISSINGNO", "role": "student", "surname": "Doe", "first_name": "Jane",
        })
        self.assertFalse(missing.json["success"])
        self.assertIn("required", missing.json["message"])

        first = self.register_card({
            "uid": "NUMBER1", "role": "student", "surname": "Doe", "first_name": "Jane",
            "student_number": "S-001",
        })
        duplicate = self.register_card({
            "uid": "NUMBER2", "role": "student", "surname": "Roe", "first_name": "John",
            "student_number": "s-001",
        })
        self.assertTrue(first.json["success"], first.json)
        self.assertFalse(duplicate.json["success"])
        self.assertIn("student number", duplicate.json["message"])

    def test_cooldown_does_not_change_open_record_or_restart_on_rejection(self):
        from unittest import mock
        self.student("AA11", claim=False)
        with mock.patch("database.unix_time", side_effect=[2000.0, 2020.0, 2030.0]):
            self.assertEqual(self.tap("AA11").json["result"], "TIME_IN_OK")
            last_successful_tap = db.uid_exists("AA11")["last_successful_tap"]
            db.init_db()
            rejected = self.tap("AA11").json
            self.assertEqual(db.uid_exists("AA11")["last_successful_tap"], last_successful_tap)
            accepted = self.tap("AA11").json
        self.assertEqual(rejected["result"], "REJECT_COOLDOWN")
        self.assertEqual(rejected["cooldown_seconds"], 10)
        self.assertEqual(accepted["result"], "TIME_OUT_OK")
        with closing(db.get_conn()) as c:
            rows = c.execute("SELECT time_in, time_out FROM logs WHERE user_id = ?", (
                db.uid_exists("AA11")["id"],
            )).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["time_out"])

    def test_simultaneous_taps_create_only_one_attendance_action(self):
        from unittest import mock
        self.student("RACE1", claim=False)

        def tap_from_separate_client():
            return flask_app.app.test_client().post("/tap", json={"uid": "RACE1"}).json["result"]

        with mock.patch("database.unix_time", return_value=3000.0):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: tap_from_separate_client(), range(2)))

        self.assertCountEqual(results, ["TIME_IN_OK", "REJECT_COOLDOWN"])
        with closing(db.get_conn()) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM logs WHERE user_id = ?", (db.uid_exists("RACE1")["id"],)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_legacy_database_migration_preserves_records_without_assigning_numbers(self):
        temp = tempfile.TemporaryDirectory()
        previous_db_name = db.DB_NAME
        db.DB_NAME = os.path.join(temp.name, "legacy.db")
        legacy = sqlite3.connect(db.DB_NAME)
        legacy.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, rfid_uid TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL, role TEXT NOT NULL, is_temp INTEGER NOT NULL DEFAULT 0,
                time_limit_minutes INTEGER, is_expired INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                registered_at TEXT NOT NULL DEFAULT '2025-01-01 08:00:00'
            );
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                time_in TEXT, time_out TEXT, expected_out TEXT, flag TEXT
            );
            INSERT INTO users (rfid_uid, name, role) VALUES ('OLD1', 'Legacy Student', 'student');
            INSERT INTO logs (user_id, time_in) VALUES (1, '2025-01-02 08:00:00');
        """)
        legacy.close()
        try:
            db.init_db()
            old = db.uid_exists("OLD1")
            with closing(db.get_conn()) as c:
                log = c.execute("SELECT id, time_in FROM logs WHERE user_id = 1").fetchone()
            self.assertEqual((old["name"], old["rfid_uid"]), ("Legacy Student", "OLD1"))
            self.assertIsNone(old["student_number"])
            self.assertEqual((log["id"], log["time_in"]), (1, "2025-01-02 08:00:00"))

            db.add_user("NEW1", "New Student", "student", student_number="ADMIN-001")
            self.assertEqual(db.uid_exists("NEW1")["student_number"], "ADMIN-001")
            db.delete_user("OLD1")
            db.add_user("OLD1", "Replacement Student", "student", student_number="ADMIN-002")
            replacement = db.uid_exists("OLD1")
            self.assertNotEqual(replacement["id"], old["id"])
            self.assertEqual(replacement["student_number"], "ADMIN-002")
            with closing(db.get_conn()) as c:
                preserved_log = c.execute(
                    "SELECT user_id, time_in FROM logs WHERE id = 1"
                ).fetchone()
            self.assertEqual(
                (preserved_log["user_id"], preserved_log["time_in"]),
                (old["id"], "2025-01-02 08:00:00"),
            )
        finally:
            db.DB_NAME = previous_db_name
            temp.cleanup()

    def test_tap_survives_notifier_failure(self):
        from unittest import mock
        self.student("AA11", claim=False)
        with mock.patch("api.push.submit", side_effect=RuntimeError("boom")):
            r = self.tap("AA11")
        self.assertEqual(r.json["result"], "TIME_IN_OK")


class AuthFlow(Base):
    def test_claim_login_me(self):
        s = self.student()
        me = self.auth("get", "/me", s["access"]).json
        self.assertEqual((me["name"], me["student_number"], me["course_section"]), ("Juan", "2023-0142", "BSIT - 2A"))
        self.assertEqual(me["rfid"]["status"], "registered")
        self.assertEqual(me["unread_count"], 1)  # welcome notification
        self.assertNotIn("uid", str(me).lower().replace("student", ""))
        r = self.cl.post("/api/v1/auth/login", json={"student_number": "2023-0142", "password": s["pw"]})
        self.assertEqual(r.status_code, 200)
        r = self.cl.post("/api/v1/auth/login", json={"student_number": "2023-0142", "password": "wrong-pass"})
        self.assertEqual(r.status_code, 401)

    def test_claim_code_single_use_and_wrong_code_lockout(self):
        s = self.student(claim=False)
        code = self.admin("post", f"/admin/students/{s['user_id']}/claim-code").json["claim_code"]
        for _ in range(5):
            r = self.cl.post("/api/v1/auth/claim", json={"student_number": s["sn"], "claim_code": "AAAA-BBBB",
                                                         "password": "longenough1"})
            self.assertEqual(r.status_code, 400)
        r = self.cl.post("/api/v1/auth/claim", json={"student_number": s["sn"], "claim_code": code,
                                                     "password": "longenough1"})
        self.assertEqual(r.status_code, 400, "code must be locked after 5 bad attempts")

    def test_claim_cannot_be_reused_or_hijack_claimed_account(self):
        s = self.student()
        r = self.cl.post("/api/v1/auth/claim", json={"student_number": s["sn"], "claim_code": s["code"],
                                                     "password": "another-pass1"})
        self.assertEqual(r.status_code, 400)

    def test_visitors_cannot_get_accounts(self):
        self.register_card({"uid": "VV1", "name": "Guest", "role": "visitor"})
        vid = db.uid_exists("VV1")["id"]
        self.assertEqual(self.admin("put", f"/admin/students/{vid}/profile",
                                    json={"student_number": "V-1"}).status_code, 404)

    def test_login_rate_limit(self):
        s = self.student()
        codes = [self.cl.post("/api/v1/auth/login", json={"student_number": s["sn"], "password": "bad-password"})
                 .status_code for _ in range(10)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[8], 429)

    def test_refresh_rotation_and_replay_detection(self):
        s = self.student()
        r = self.cl.post("/api/v1/auth/refresh", json={"refresh_token": s["refresh"]})
        self.assertEqual(r.status_code, 200)
        new_access, new_refresh = r.json["access_token"], r.json["refresh_token"]
        self.assertEqual(self.auth("get", "/me", new_access).status_code, 200)
        # replaying the old refresh token revokes the whole session
        self.assertEqual(self.cl.post("/api/v1/auth/refresh", json={"refresh_token": s["refresh"]}).status_code, 401)
        self.assertEqual(self.auth("get", "/me", new_access).status_code, 401)
        self.assertEqual(self.cl.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh}).status_code, 401)

    def test_expired_access_token_and_logout(self):
        s = self.student()
        with closing(db.get_conn()) as c:
            c.execute("UPDATE api_sessions SET access_expires = '2000-01-01 00:00:00'")
        r = self.auth("get", "/me", s["access"])
        self.assertEqual((r.status_code, r.json["error"]["code"]), (401, "token_expired"))
        r = self.cl.post("/api/v1/auth/refresh", json={"refresh_token": s["refresh"]})
        tok = r.json["access_token"]
        self.assertEqual(self.auth("post", "/auth/logout", tok).status_code, 200)
        self.assertEqual(self.auth("get", "/me", tok).status_code, 401)

    def test_endpoints_require_auth_and_admin_key(self):
        for path in ("/me", "/home", "/attendance", "/attendance/today", "/notifications", "/contacts"):
            self.assertEqual(self.cl.get("/api/v1" + path).status_code, 401, path)
        self.assertEqual(self.cl.get("/api/v1/admin/students").status_code, 401)
        r = self.cl.get("/api/v1/admin/students", headers={"X-Admin-Key": "wrong-key-wrong-key-1"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.cl.get("/api/v1/nope").json["error"]["code"], "not_found")

    def test_admin_disabled_without_key(self):
        old = os.environ.pop("ADMIN_API_KEY")
        try:
            self.assertEqual(self.cl.get("/api/v1/admin/students").status_code, 503)
        finally:
            os.environ["ADMIN_API_KEY"] = old


class Attendance(Base):
    def test_today_states_follow_real_taps(self):
        s = self.student()
        t = self.auth("get", "/attendance/today", s["access"]).json
        self.assertEqual((t["state"], t["expected_scan"], t["time_in"]), ("not_in", "time_in", None))
        self.tap(s["uid"])
        t = self.auth("get", "/attendance/today", s["access"]).json
        self.assertEqual((t["state"], t["status"], t["expected_scan"]), ("in", "pending", "time_out"))
        self.assertTrue(t["time_in"].endswith("+08:00"))
        from unittest import mock
        last_tap = db.uid_exists(s["uid"])["last_successful_tap"]
        with mock.patch("database.unix_time", return_value=last_tap + 60):
            self.tap(s["uid"])
        t = self.auth("get", "/attendance/today", s["access"]).json
        self.assertEqual((t["state"], t["status"], t["expected_scan"]), ("out", "complete", None))
        self.assertIsNotNone(t["time_out"])
        self.settle()

    def test_status_mapping_from_logs(self):
        s = self.student()
        uid = s["user_id"]
        with closing(db.get_conn()) as c:
            c.executemany("INSERT INTO logs (user_id, time_in, time_out, flag) VALUES (?,?,?,?)", [
                (uid, "2026-09-01 07:40:00", "2026-09-01 16:30:00", None),            # complete
                (uid, "2026-09-02 07:41:00", None, "MISSING_TIME_OUT"),               # incomplete (flagged)
                (uid, "2026-09-03 07:42:00", None, None),                              # stale open -> incomplete
                (uid, "2026-09-04 07:10:00", "2026-09-04 10:00:00", None),            # two sessions, complete
                (uid, "2026-09-04 12:00:00", "2026-09-04 16:00:00", None),
            ])
        days = {d["date"]: d for d in self.auth("get", "/attendance?month=2026-09", s["access"]).json["days"]}
        self.assertEqual([days[k]["status"] for k in sorted(days)],
                         ["complete", "incomplete", "incomplete", "complete"])
        self.assertEqual(len(days["2026-09-04"]["sessions"]), 2)
        self.assertEqual(days["2026-09-04"]["time_in"][:19], "2026-09-04T07:10:00")
        self.assertEqual(days["2026-09-04"]["time_out"][:19], "2026-09-04T16:00:00")
        r = self.auth("get", "/attendance/2026-09-10", s["access"]).json
        self.assertEqual((r["status"], r["sessions"]), ("unavailable", []))
        self.assertEqual(self.auth("get", "/attendance?from=2026-09-02&to=2026-09-03", s["access"])
                         .json["summary"]["incomplete"], 2)

    def test_input_validation(self):
        s = self.student()
        for q in ("?month=2026-13", "?month=abc", "?from=2026-01-01", "?from=2026-01-05&to=2026-01-01"):
            self.assertEqual(self.auth("get", "/attendance" + q, s["access"]).status_code, 400, q)
        self.assertEqual(self.auth("get", "/attendance/not-a-date", s["access"]).status_code, 400)

    def test_students_only_see_their_own_records(self):
        a = self.student("AA11", "Ana", "S-1")
        b = self.student("BB22", "Ben", "S-2")
        self.tap("BB22")
        self.assertEqual(self.auth("get", "/attendance/today", a["access"]).json["state"], "not_in")
        self.assertEqual(self.auth("get", "/attendance/today", b["access"]).json["state"], "in")
        self.assertEqual(self.auth("get", "/home", a["access"]).json["recent"], [])
        self.settle()

    def test_no_write_path_to_attendance(self):
        s = self.student()
        for m in ("post", "put", "patch", "delete"):
            r = self.auth(m, "/attendance/today", s["access"], json={"time_in": "x"})
            self.assertIn(r.status_code, (404, 405))


class Notifications(Base):
    def test_confirmed_notification_uses_the_exact_log_id(self):
        """A delayed worker must not notify against a newer attendance record."""
        from api import notifier

        s = self.student()
        with closing(db.get_conn()) as c:
            first_id = c.execute(
                "INSERT INTO logs (user_id, time_in, time_out) VALUES (?, ?, ?)",
                (s["user_id"], "2026-10-16 08:00:00", "2026-10-16 12:00:00"),
            ).lastrowid
            c.execute(
                "INSERT INTO logs (user_id, time_in, time_out) VALUES (?, ?, ?)",
                (s["user_id"], "2026-10-16 13:00:00", "2026-10-16 17:00:00"),
            )

        notification_id = notifier.process_tap("TIME_OUT_OK", s["user_id"], first_id)
        with closing(db.get_conn()) as c:
            notification = c.execute(
                "SELECT log_id, body FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        self.assertEqual(notification["log_id"], first_id)
        self.assertIn("12:00 PM", notification["body"])

    def test_confirmed_events_create_one_notification_and_push(self):
        s = self.student()
        self.register_device(s)
        self.tap(s["uid"])
        self.settle()
        items = self.auth("get", "/notifications?category=attendance", s["access"]).json["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Attendance Recorded")
        self.assertIn("time in", items[0]["body"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["to"], TOKEN)
        from unittest import mock
        last_tap = db.uid_exists(s["uid"])["last_successful_tap"]
        with mock.patch("database.unix_time", return_value=last_tap + 60):
            self.tap(s["uid"])
        self.settle()
        self.assertEqual(len(self.auth("get", "/notifications?category=attendance", s["access"]).json["items"]), 2)
        self.assertEqual(len(self.sent), 2)

    def test_duplicate_scans_produce_a_single_warning(self):
        s = self.student()
        self.register_device(s)
        self.tap(s["uid"])
        for _ in range(5):
            self.assertEqual(self.tap(s["uid"]).json["result"], "REJECT_COOLDOWN")
        self.settle()
        titles = [i["title"] for i in self.auth("get", "/notifications", s["access"]).json["items"]]
        self.assertEqual(titles.count("Duplicate Scan"), 0)
        self.assertEqual(titles.count("Attendance Recorded"), 1)
        self.assertEqual(len(self.sent), 1)

    def test_tap_without_mode_automatically_starts_attendance(self):
        s = self.student()
        self.assertEqual(self.tap(s["uid"]).json["result"], "TIME_IN_OK")
        self.settle()
        items = self.auth("get", "/notifications", s["access"]).json["items"]
        self.assertEqual(items[0]["title"], "Attendance Recorded")

    def test_no_notification_for_unknown_cards_visitors_or_unclaimed_students(self):
        self.tap("UNKNOWN")
        self.register_card({"uid": "VV1", "name": "Guest", "role": "visitor"})
        self.tap("VV1")
        self.student("CC33", "Cy", "S-3", claim=False)
        self.tap("CC33")
        self.settle()
        with closing(db.get_conn()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)

    def test_preferences_gate_push_but_not_inbox_or_attendance(self):
        s = self.student()
        self.register_device(s)
        r = self.auth("patch", "/me/preferences", s["access"], json={"attendance_alerts": False})
        self.assertFalse(r.json["preferences"]["attendance_alerts"])
        self.assertTrue(r.json["preferences"]["emergency_alerts"])
        self.assertEqual(self.tap(s["uid"]).json["result"], "TIME_IN_OK")  # official record unaffected
        self.settle()
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.auth("get", "/notifications?category=attendance", s["access"]).json["items"]), 1)
        for bad in ({"emergency_alerts": False}, {"sound": "no"}, {}):
            self.assertEqual(self.auth("patch", "/me/preferences", s["access"], json=bad).status_code, 400)

    def test_emergency_announcement_ignores_optional_prefs(self):
        s = self.student()
        self.register_device(s)
        self.auth("patch", "/me/preferences", s["access"], json={"announcements": False})
        r = self.admin("post", "/admin/announcements",
                       json={"category": "emergency", "title": "Class suspended", "body": "Stay home."})
        self.assertEqual((r.status_code, r.json["recipients"]), (201, 1))
        self.admin("post", "/admin/announcements", json={"category": "system", "title": "Update", "body": "x"})
        self.settle()
        self.assertEqual([m["title"] for m in self.sent], ["Class suspended"])

    def test_inbox_isolation_read_state_and_pagination(self):
        a = self.student("AA11", "Ana", "S-1")
        b = self.student("BB22", "Ben", "S-2")
        a_ids = [i["id"] for i in self.auth("get", "/notifications", a["access"]).json["items"]]
        b_ids = [i["id"] for i in self.auth("get", "/notifications", b["access"]).json["items"]]
        self.assertTrue(a_ids and b_ids and not set(a_ids) & set(b_ids))
        # IDOR: Ana cannot read or mark Ben's notification
        self.assertEqual(self.auth("get", f"/notifications/{b_ids[0]}", a["access"]).status_code, 404)
        self.assertEqual(self.auth("post", f"/notifications/{b_ids[0]}/read", a["access"]).status_code, 404)
        self.assertEqual(self.auth("get", "/notifications/unread-count", b["access"]).json["total"], 1)
        r = self.auth("post", f"/notifications/{a_ids[0]}/read", a["access"])
        self.assertTrue(r.json["read"])
        self.assertEqual(self.auth("get", "/notifications/unread-count", a["access"]).json["total"], 0)
        for i in range(5):
            self.admin("post", "/admin/announcements", json={"category": "system", "title": f"n{i}", "body": "b"})
        self.settle()
        p1 = self.auth("get", "/notifications?limit=3", a["access"]).json
        self.assertEqual(len(p1["items"]), 3)
        p2 = self.auth("get", f"/notifications?limit=3&before={p1['next_before']}", a["access"]).json
        self.assertEqual(len(p2["items"]), 3)
        self.assertEqual(self.auth("post", "/notifications/read-all", a["access"]).json["updated"], 5)
        self.assertEqual(self.auth("get", "/notifications/unread-count", b["access"]).json["total"], 6)

    def test_device_not_registered_disables_token(self):
        s = self.student()
        self.register_device(s)
        push.send_messages = lambda msgs: [{"status": "error", "message": "gone",
                                            "details": {"error": "DeviceNotRegistered"}} for _ in msgs]
        self.tap(s["uid"])
        self.settle()
        with closing(db.get_conn()) as c:
            self.assertIsNotNone(c.execute("SELECT disabled_at FROM push_devices").fetchone()[0])

    def test_transient_push_failure_is_retried_once_per_notification(self):
        s = self.student()
        self.register_device(s)
        self.fail_next = [push.Transient("network down")]
        self.tap(s["uid"])
        self.settle()
        self.assertEqual(len(self.sent), 1)  # retried exactly once, no duplicates
        with closing(db.get_conn()) as c:
            row = c.execute("SELECT push_status, push_attempts FROM notifications WHERE category='attendance'").fetchone()
        self.assertEqual(row["push_status"], "sent")

    def test_receipt_check_disables_unregistered_device(self):
        s = self.student()
        self.register_device(s)
        self.tap(s["uid"])
        self.settle()
        with closing(db.get_conn()) as c:
            c.execute("UPDATE push_tickets SET created_at = '2000-01-01 00:00:00'")
        push.get_receipts = lambda ids: {i: {"status": "error", "details": {"error": "DeviceNotRegistered"}}
                                         for i in ids}
        push.check_receipts()
        with closing(db.get_conn()) as c:
            self.assertIsNotNone(c.execute("SELECT disabled_at FROM push_devices").fetchone()[0])


class DevicesContactsAdmin(Base):
    def test_device_validation_and_reassignment(self):
        a = self.student("AA11", "Ana", "S-1")
        b = self.student("BB22", "Ben", "S-2")
        bad = self.auth("post", "/devices", a["access"], json={"token": "not-a-token-at-all-xxxxxxxx",
                                                               "platform": "android"})
        self.assertEqual(bad.status_code, 400)
        self.register_device(a)
        self.register_device(b)  # same phone, different student: token moves to Ben
        with closing(db.get_conn()) as c:
            self.assertEqual(c.execute("SELECT user_id FROM push_devices WHERE token = ?", (TOKEN,)).fetchone()[0],
                             b["user_id"])
        self.auth("post", "/devices/unregister", b["access"], json={"token": TOKEN})
        with closing(db.get_conn()) as c:
            self.assertIsNotNone(c.execute("SELECT disabled_at FROM push_devices").fetchone()[0])

    def test_contacts_admin_managed_and_no_invented_numbers(self):
        s = self.student()
        self.assertEqual(self.auth("get", "/contacts", s["access"]).json["items"], [])  # ships empty
        r = self.admin("post", "/admin/contacts", json={"role": "Adviser", "name": "Ms. Reyes",
                                                        "phone": "+63 912 345 6789", "notes": "Mon-Fri 8-5"})
        self.assertEqual(r.status_code, 201)
        cid = r.json["id"]
        self.assertEqual(self.admin("post", "/admin/contacts",
                                    json={"role": "x", "name": "y", "phone": "call me"}).status_code, 400)
        items = self.auth("get", "/contacts", s["access"]).json["items"]
        self.assertEqual((items[0]["role"], items[0]["notes"]), ("Adviser", "Mon-Fri 8-5"))
        self.admin("patch", f"/admin/contacts/{cid}", json={"phone": "+63 917 123 4567"})
        self.assertEqual(self.auth("get", "/contacts", s["access"]).json["items"][0]["phone"], "+63 917 123 4567")
        self.admin("delete", f"/admin/contacts/{cid}")
        self.assertEqual(self.auth("get", "/contacts", s["access"]).json["items"], [])

    def test_reset_account_and_user_delete_cleanup(self):
        s = self.student()
        self.register_device(s)
        self.assertEqual(self.admin("post", f"/admin/students/{s['user_id']}/claim-code").status_code, 409)
        self.assertEqual(self.admin("post", f"/admin/students/{s['user_id']}/reset-account").status_code, 200)
        self.assertEqual(self.auth("get", "/me", s["access"]).status_code, 401)
        code = self.admin("post", f"/admin/students/{s['user_id']}/claim-code").json["claim_code"]
        r = self.cl.post("/api/v1/auth/claim", json={"student_number": s["sn"], "claim_code": code.lower(),
                                                     "password": "brand new pass"})
        self.assertEqual(r.status_code, 201)
        self.register_device({"access": r.json["access_token"]}, TOKEN2)
        self.cl.delete("/users/" + s["uid"])
        with closing(db.get_conn()) as c:
            for t in ("student_accounts", "student_profiles", "api_sessions", "notifications", "push_devices",
                      "notification_prefs"):
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 0, t)

    def test_duplicate_student_number_rejected(self):
        self.student("AA11", "Ana", "S-1", claim=False)
        self.register_card({
            "uid": "BB22", "name": "Ben", "role": "student", "student_number": "S-2",
        })
        bid = db.uid_exists("BB22")["id"]
        self.assertEqual(self.admin("put", f"/admin/students/{bid}/profile",
                                    json={"student_number": "s-1"}).status_code, 409)

    def test_claim_code_stored_hashed_only(self):
        s = self.student(claim=False)
        code = self.admin("post", f"/admin/students/{s['user_id']}/claim-code").json["claim_code"]
        with closing(db.get_conn()) as c:
            stored = c.execute("SELECT code_hash FROM claim_codes").fetchone()[0]
        self.assertNotIn(code.replace("-", ""), stored)
        self.assertEqual(len(stored), 64)

    def test_health(self):
        r = self.cl.get("/api/v1/health")
        self.assertEqual(r.json["status"], "ok")


if __name__ == "__main__":
    unittest.main()
