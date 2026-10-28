# Mobile API (`/api/v1`) — setup and reference

Adds a student-facing API to the existing Flask server. The ESP32 flow is unchanged:
`POST /tap` takes the same `{"uid": "..."}` and returns the same JSON as before.

## What changed in your project

| File | Change |
|---|---|
| `api/` (new) | The `/api/v1` blueprint, notification worker, schema |
| `app.py` | 4 small edits: import, one `notify_after_tap(code, user)` line after `record_tap`, `purge_user_data` in `DELETE /users/<uid>`, `init_api(app)` before `__main__` |
| `database.py` | **Not modified.** New tables are created by `api/schema.py` (`CREATE TABLE IF NOT EXISTS`, plus one index on `logs(user_id, time_in)`) |
| `tools/school_admin.py` (new) | CLI for claim codes, contacts, announcements |
| `tests/test_api.py` (new) | 32 tests against a temporary database |

`notify_after_tap` only appends to an in-memory queue. A background thread writes the inbox row and calls Expo,
so a slow or failing notification can never delay or change the ESP32 response. If the server restarts, pushes
that were created but not yet delivered (last 2 hours) are retried; a tap that was queued but not yet turned into
an inbox row at the moment of a crash is not notified (the attendance record itself is unaffected).

## Environment variables (server)

| Variable | Required | Purpose |
|---|---|---|
| `ADMIN_API_KEY` | yes, for admin routes | 16+ random characters. Admin routes return 503 until set. Never put it in the app |
| `PUSH_ENABLED` | no (default `1`) | `0` keeps inbox notifications but sends no push |
| `EXPO_ACCESS_TOKEN` | no | Only if you enable "enhanced push security" in your Expo account |
| `ACCESS_TOKEN_MINUTES` / `REFRESH_TOKEN_DAYS` | no (60 / 30) | Session lifetimes |

Generate a key: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

Run: `set ADMIN_API_KEY=...` then `python app.py` (already binds `0.0.0.0:5001`).

## Onboarding a student (claim codes)

1. Register the student's card as you do today (`POST /users`, role `student`).
2. `python tools/school_admin.py students` → find the `user_id`.
3. `python tools/school_admin.py set-profile 12 2023-0142 --section "BSIT - 2A" --phone "+63..."`
4. `python tools/school_admin.py claim-code 12` → give the code to the student in person.
5. In the app: student number + claim code + new password. Codes work once, expire (default 72 h), lock after 5 wrong tries and are stored hashed.

Forgot password: `reset-account <user_id>`, then issue a new claim code.
Contacts: `add-contact "Adviser" "Ms. Reyes" "+63 912 345 6789" --notes "Mon-Fri 8-5"`. The list ships **empty**; nothing is invented.

## Endpoints

All student routes need `Authorization: Bearer <access_token>`; the student is taken from the token, never from a parameter.
Errors are `{"error": {"code": "...", "message": "..."}}`. Times are ISO 8601 with `+08:00`.

| Method & path | Purpose |
|---|---|
| `GET /health` | Connectivity check (no auth) |
| `POST /auth/claim` · `/auth/login` · `/auth/refresh` · `/auth/logout` | Auth. Refresh tokens rotate; replaying an old one revokes the session |
| `GET /me` · `PATCH /me/preferences` | Profile (read-only), `attendance_alerts` / `announcements` / `sound` / `haptics` |
| `GET /home` | Profile + today + last 5 days + unread count in one call |
| `GET /attendance/today` | `state`: `not_in` / `in` / `out`; `expected_scan`: `time_in` / `time_out` / null |
| `GET /attendance?month=YYYY-MM` or `?from=&to=` | Days with sessions (max 366 days) |
| `GET /attendance/<YYYY-MM-DD>` | One day; `unavailable` when no record exists |
| `GET /notifications?category=&unread=1&limit=&before=` · `/unread-count` · `/<id>` | Inbox |
| `POST /notifications/<id>/read` · `/notifications/read-all` | Mark read |
| `POST /devices` · `/devices/unregister` | Expo push tokens |
| `GET /contacts` | School-approved contacts |
| `/admin/*` | Admin (`X-Admin-Key`): students, profile, claim-code, reset-account, contacts, announcements |

Attendance status: `complete` (has time out), `pending` (open record today), `incomplete` (flagged `MISSING_TIME_OUT`, or an open
record from a past day), `unavailable` (no log). There is no endpoint that writes to `logs`.

## Notifications

| Tap result | Inbox + push |
|---|---|
| `TIME_IN_OK` / `TIME_OUT_OK` | "Attendance Recorded", one per log row (`log:<id>:IN/OUT` is unique) |
| `ALREADY_CHECKED_IN` | One "Duplicate Scan" per student per 10 minutes, not one per tap |
| `REJECT_NO_OPEN_RECORD` | One "Attendance Issue" per 10 minutes |
| unknown card, visitors, unclaimed students | Nothing |

Optional preferences only gate **push**; the inbox and the official attendance record are never affected. `emergency`
announcements ignore the optional switches. Invalid tokens (`DeviceNotRegistered`) are disabled from both the push
ticket and the later receipt check; network/5xx/rate-limit errors retry after 5 s, 30 s, 2 min, 10 min.

**Push setup.** Phones need internet and the server needs outbound HTTPS to `exp.host`. In the Expo app, the token comes from
`getExpoPushTokenAsync`; production Android builds also need Firebase (FCM) credentials uploaded to Expo (see Expo's push
notifications setup guide). Expo Go can receive pushes during development on supported SDKs.

## Networking and security

- **Phones cannot reach `localhost`.** Use the PC's LAN IP (e.g. `http://192.168.1.20:5001`), phone on the same Wi-Fi, firewall allowing port 5001.
  For off-campus use, put the server behind an HTTPS reverse proxy or tunnel.
- **Do not expose the whole Flask app.** `/tap`, `/users`, `/logs` and `/display-event` are unauthenticated. At the proxy/tunnel,
  allow only `/api/v1/*` and **block `/api/v1/admin/*`**. Run the admin CLI on the school PC.
- `/tap` itself is still unauthenticated (unchanged on purpose). Anyone on the LAN can post a UID, so keep the network trusted.
- Rate limits and the login/claim counters are in memory, per process; use a single server process (the default `app.run`).
- If you sit behind a proxy, `request.remote_addr` is the proxy's address, so per-IP limits become global; tell me and I'll add
  trusted-proxy handling.
- Keep the ESP32 Wi-Fi credentials out of git: move them into an uncommitted `secrets.h`.

## Run the tests

```
python -m unittest tests.test_api
```
