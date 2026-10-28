import os
import sys
import tempfile
import unittest
from contextlib import closing

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

    def student(self, uid="AA11", name="Juan", sn="2023-0142", claim=True, pw="correct horse"):
        self.assertTrue(self.cl.post("/users", json={"uid": uid, "name": name, "role": "student"}).json["success"])
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
    def test_tap_response_shape_unchanged(self):
        r = self.tap("NOPE")
        self.assertEqual(r.json, {"status": "denied", "result": "DENY_UNKNOWN", "name": "", "role": "",
                                  "action": "", "message": "Card not registered. Please see staff."})
        self.student("AA11", claim=False)
        from unittest import mock
        with mock.patch("database.unix_time", side_effect=[1000, 1010, 1030]):
            first = self.tap("aa11")
            duplicate = self.tap("AA11")
            timed_out = self.tap("AA11")
        self.assertEqual(set(first.json), {"status", "result", "name", "role", "action", "message"})
        self.assertEqual((first.json["status"], first.json["result"], first.json["action"]),
                         ("granted", "TIME_IN_OK", "IN"))
        self.assertEqual((duplicate.json["result"], duplicate.json["cooldown_seconds"]),
                         ("REJECT_COOLDOWN", 20))
        self.assertEqual((timed_out.json["result"], timed_out.json["action"]), ("TIME_OUT_OK", "OUT"))
        self.assertEqual(self.cl.post("/timeout-arm").status_code, 404)

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
        self.cl.post("/users", json={"uid": "VV1", "name": "Guest", "role": "visitor"})
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
        from unittest import mock
        with mock.patch("database.unix_time", return_value=2000):
            self.tap(s["uid"])
        t = self.auth("get", "/attendance/today", s["access"]).json
        self.assertEqual((t["state"], t["status"], t["expected_scan"]), ("in", "pending", "time_out"))
        self.assertTrue(t["time_in"].endswith("+08:00"))
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
        with mock.patch("database.unix_time", return_value=db.uid_exists(s["uid"])["last_successful_tap"] + 60):
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
        self.cl.post("/users", json={"uid": "VV1", "name": "Guest", "role": "visitor"})
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
        self.cl.post("/users", json={"uid": "BB22", "name": "Ben", "role": "student"})
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
