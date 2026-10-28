#!/usr/bin/env python3
"""Tiny admin CLI for the mobile API. Run it on the school PC (never ship the key in the app).

  set ADMIN_API_KEY=...   (and optionally SCHOOL_API=http://127.0.0.1:5001)
  python tools/school_admin.py students [search]
  python tools/school_admin.py set-profile <user_id> <student_number> [--section "BSIT - 2A"] [--phone "+63..."]
  python tools/school_admin.py claim-code <user_id> [--hours 72]
  python tools/school_admin.py reset-account <user_id>
  python tools/school_admin.py contacts
  python tools/school_admin.py add-contact "<role>" "<name>" "<phone>" [--notes "Mon-Fri 8-5"] [--order 1]
  python tools/school_admin.py remove-contact <id>
  python tools/school_admin.py announce <emergency|system> "<title>" "<body>"
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("SCHOOL_API", "http://127.0.0.1:5001").rstrip("/") + "/api/v1"


def call(method, path, body=None):
    key = os.environ.get("ADMIN_API_KEY")
    if not key:
        sys.exit("Set the ADMIN_API_KEY environment variable first.")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data, method=method,
                                 headers={"X-Admin-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode())["error"]["message"]
        except Exception:
            msg = e.reason
        sys.exit(f"Error {e.code}: {msg}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {BASE}: {e.reason}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("students"); s.add_argument("search", nargs="?", default="")
    s = sub.add_parser("set-profile"); s.add_argument("user_id", type=int); s.add_argument("student_number")
    s.add_argument("--section"); s.add_argument("--phone")
    s = sub.add_parser("claim-code"); s.add_argument("user_id", type=int); s.add_argument("--hours", type=int, default=72)
    s = sub.add_parser("reset-account"); s.add_argument("user_id", type=int)
    sub.add_parser("contacts")
    s = sub.add_parser("add-contact"); s.add_argument("role"); s.add_argument("name"); s.add_argument("phone")
    s.add_argument("--notes"); s.add_argument("--order", type=int, default=0)
    s = sub.add_parser("remove-contact"); s.add_argument("id", type=int)
    s = sub.add_parser("announce"); s.add_argument("category", choices=["emergency", "system"])
    s.add_argument("title"); s.add_argument("body")
    a = p.parse_args()

    if a.cmd == "students":
        for r in call("GET", "/admin/students?q=" + urllib.parse.quote(a.search))["items"]:
            state = "claimed" if r["claimed"] else ("ready" if r["has_profile"] else "no profile")
            print(f'{r["user_id"]:>4}  {r["name"]:<28} {r["student_number"] or "-":<14} {state}')
    elif a.cmd == "set-profile":
        body = {"student_number": a.student_number, "course_section": a.section, "phone": a.phone}
        call("PUT", f"/admin/students/{a.user_id}/profile", {k: v for k, v in body.items() if v is not None})
        print("Profile saved.")
    elif a.cmd == "claim-code":
        r = call("POST", f"/admin/students/{a.user_id}/claim-code", {"expires_hours": a.hours})
        print(f'Claim code for {r["student_number"]}: {r["claim_code"]}   (expires {r["expires_at"]})')
        print("Give it to the student in person. It is shown only once and works only once.")
    elif a.cmd == "reset-account":
        call("POST", f"/admin/students/{a.user_id}/reset-account"); print("Account reset. Issue a new claim code.")
    elif a.cmd == "contacts":
        for r in call("GET", "/admin/contacts")["items"]:
            print(f'{r["id"]:>3} {"on " if r["active"] else "off"} {r["role"]:<22} {r["name"]:<24} {r["phone"]}  {r["notes"] or ""}')
    elif a.cmd == "add-contact":
        body = {"role": a.role, "name": a.name, "phone": a.phone, "sort_order": a.order}
        if a.notes:
            body["notes"] = a.notes
        print("Added contact", call("POST", "/admin/contacts", body)["id"])
    elif a.cmd == "remove-contact":
        call("DELETE", f"/admin/contacts/{a.id}"); print("Contact hidden from the app.")
    elif a.cmd == "announce":
        print("Sent to", call("POST", "/admin/announcements",
                              {"category": a.category, "title": a.title, "body": a.body})["recipients"], "students.")


if __name__ == "__main__":
    main()
