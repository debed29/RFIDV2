# RFID Tap In/Out System — Complete Build & Execution Guide

> **For the executing agent:** This document contains everything needed to build a complete
> RFID tap-in/tap-out attendance system. Follow the phases in order. Create all files exactly
> as specified. Do not add features beyond what is written here.
> **Current source note:** The embedded source listings below describe the original toggle-based
> starter version and are historical. Do not copy them over the current project files. The
> checked-in Python and ESP32 files are the source of truth for the workflow summarized here.


## 1. Project Overview

An RFID attendance system where:

- A user taps an RFID card on an **RFID-RC522** reader connected to an **ESP32**.
- The ESP32 sends the card's UID over WiFi to a **Flask server** running on a PC.
 The server checks the UID in a local **SQLite database**:
    - **Registered card** → the server checks the database: no open record means TIME IN; an open record means TIME OUT after that card's 30-second cooldown.
    - **Unknown card** → denied and must be registered by staff.
    - **Visitor** → the same automatic in/out and per-card cooldown applies; completed visitor cards expire, and overstay/missing-time-out records are flagged.
- The ESP32 displays the same scan outcome, name, role, and message as the student display on a **16x2 I2C LCD**, cycling between the summary and detail to fit the smaller screen.
 A **Tkinter desktop UI** on the PC is used to register cards (student or visitor),
    view registered cards, and review attendance/visitor flags.
    - A **PySide6 student display** shows server status and scan results without attendance controls.

### Architecture

```
[RFID Card] → [RC522] → [ESP32] --WiFi/HTTP POST--> [Flask Server (PC)] --> [SQLite DB]
                            |                              |
                       [LCD 16x2] <----JSON response-------+

[Tkinter Admin UI (PC)] <---HTTP/JSON---> [Flask Server]
```

### Tap Logic (automatic, server-authoritative)

- With no active attendance record, an eligible card records TIME IN.
- With an active record, the next eligible tap records TIME OUT; no screen button or selected mode is needed.
- Each card has a persisted 30-second cooldown after a successful attendance action. Rejected taps report the remaining time without changing the log or extending the cooldown.
- Unknown cards are denied. Staff register them through the admin UI.
- Student and visitor registration use separate Surname, First Name, and Middle Name fields and display names in that order. Student numbers are entered by the admin and required for students; visitor numbers are optional. CSV exports include each name part, and legacy full-name-only records are split using their original First Middle Surname order where possible. Registration numbers remain separate from any official school number maintained in the mobile API profile.
- Existing temporary visitor records can still be upgraded. Visitors retain their time limits and are expired after checkout; late checkout is recorded and flagged as an overstay.

---

## 2. Hardware Required

| Item | Notes |
|---|---|
| ESP32 dev board | Do **not** use a separate Arduino Uno — ESP32 handles everything |
| RFID-RC522 module | Comes with cards/keyfobs |
| 16x2 LCD **with I2C backpack** | Must be the 4-pin I2C version (SDA/SCL/VCC/GND) |
| Jumper wires + breadboard | — |
| Micro-USB cable | For programming + power |

---

## 3. Wiring

### 3.1 RC522 → ESP32 (SPI)

| RC522 Pin | ESP32 Pin |
|---|---|
| SDA (SS) | GPIO 5 |
| SCK | GPIO 18 |
| MOSI | GPIO 23 |
| MISO | GPIO 19 |
| RST | 3V3 |
| 3.3V | **3V3** ⚠️ NEVER 5V/VIN — will destroy the module |
| GND | GND |
| IRQ | leave unconnected |

> With RST tied to 3V3, the sketches use the MFRC522 library's software reset (`MFRC522::UNUSED_PIN`). Do not also connect RST to GPIO 4.

### 3.2 LCD (I2C) → ESP32

| LCD Pin | ESP32 Pin |
|---|---|
| SDA | GPIO 21 |
| SCL | GPIO 22 |
| VCC | VIN (5V ok) |
| GND | GND |

> If the LCD is blank: adjust the contrast potentiometer on its back. If still blank,
> change the LCD address in the ESP32 code from `0x27` to `0x3F`.

---

## 4. Software Prerequisites

### 4.1 PC side

1. Install **Python 3.x** from https://python.org — check **"Add Python to PATH"**.
2. Open a terminal and run:
    ```bash
    pip install -r requirements.txt
    ```
   (`sqlite3` and `tkinter` are built into Python — do not pip install them.)
3. Optional but recommended: install **DB Browser for SQLite** (free) to inspect the DB file.

### 4.2 Arduino IDE side

1. Install **Arduino IDE 2.x** from https://arduino.cc.
2. Add ESP32 board support:
   - Tools → Board → Boards Manager → search `esp32` → install **"esp32 by Espressif Systems"**.
   - If the board doesn't appear, add this URL in File → Preferences → "Additional boards manager URLs":
     `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
3. Install libraries via Tools → Manage Libraries:
   - **MFRC522** by GithubCommunity
   - **LiquidCrystal I2C** by Frank de Brabander
   - **ArduinoJson** by Benoit Blanchon (v7.x)
4. Select board: **ESP32 Dev Module**, then select the correct **COM port**.

---

## 5. Project Structure

Create this folder and the files inside it exactly:

```
project/
├── requirements.txt   → Python dependencies
├── database.py        → SQLite setup and all DB functions
├── app.py             → Flask server (the brain)
├── app_ui.py          → Tkinter admin UI
├── student_display.py → public kiosk display and TIME OUT control
├── test_server.py     → isolated API tests and opt-in live check
├── rfid_reader/
│   └── rfid_reader.ino → ESP32 sketch
├── rfid_reader_no_lcd/
│   └── rfid_reader_no_lcd.ino → optional Serial Monitor-only sketch
└── rfid_system.db     → created automatically on first server run
```

---

## 6. FILE 1 — `database.py`

Create this file with EXACTLY this content:

```python
import sqlite3

DB_NAME = "rfid_system.db"

def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rfid_uid TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('student', 'visitor')),
            is_temp INTEGER NOT NULL DEFAULT 0,
            registered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            time_in TEXT,
            time_out TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    conn.commit()
    conn.close()

def uid_exists(uid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE rfid_uid = ?", (uid,)).fetchone()
    conn.close()
    return row

def add_user(uid, name, role, is_temp=0):
    conn = get_conn()
    conn.execute("INSERT INTO users (rfid_uid, name, role, is_temp) VALUES (?, ?, ?, ?)",
                 (uid, name, role, is_temp))
    conn.commit()
    conn.close()

def upgrade_temp_user(uid, name, role):
    conn = get_conn()
    conn.execute("UPDATE users SET name = ?, role = ?, is_temp = 0 WHERE rfid_uid = ?",
                 (name, role, uid))
    conn.commit()
    conn.close()

def delete_user(uid):
    conn = get_conn()
    user = conn.execute("SELECT id FROM users WHERE rfid_uid = ?", (uid,)).fetchone()
    if user:
        conn.execute("DELETE FROM logs WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def open_log_exists(user_id):
    conn = get_conn()
    row = conn.execute("SELECT id FROM logs WHERE user_id = ? AND time_out IS NULL",
                       (user_id,)).fetchone()
    conn.close()
    return row is not None

def log_time_in(user_id):
    conn = get_conn()
    conn.execute("INSERT INTO logs (user_id, time_in) VALUES (?, datetime('now','localtime'))",
                 (user_id,))
    conn.commit()
    conn.close()

def log_time_out(user_id):
    conn = get_conn()
    conn.execute("UPDATE logs SET time_out = datetime('now','localtime') "
                 "WHERE user_id = ? AND time_out IS NULL", (user_id,))
    conn.commit()
    conn.close()

def get_recent_logs(limit=30):
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.name, u.role, l.time_in, l.time_out
        FROM logs l JOIN users u ON u.id = l.user_id
        ORDER BY l.id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

---

## 7. FILE 2 — `app.py`

Create this file with EXACTLY this content:

```python
from flask import Flask, request, jsonify
import database as db

app = Flask(__name__)

# If True: any UNKNOWN card automatically gets temporary visitor access.
# If False: unknown cards are denied.
AUTO_TEMP_VISITOR = True

# Latest card tapped, so the PC UI can capture UIDs during registration
last_tap = {"uid": None, "count": 0}

@app.route("/tap", methods=["POST"])
def tap():
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip().upper()

    if not uid:
        return jsonify({"status": "error", "message": "No UID received"})

    last_tap["uid"] = uid
    last_tap["count"] += 1

    user = db.uid_exists(uid)

    if user is None and AUTO_TEMP_VISITOR:
        db.add_user(uid, "Temp Visitor", "visitor", is_temp=1)
        user = db.uid_exists(uid)

    if user is None:
        return jsonify({"status": "denied", "message": "Not registered"})

    if db.open_log_exists(user["id"]):
        db.log_time_out(user["id"])
        action = "OUT"
        message = f"Goodbye, {user['name']}!"
    else:
        db.log_time_in(user["id"])
        action = "IN"
        message = f"Welcome, {user['name']}!"

    return jsonify({
        "status": "granted",
        "name": user["name"],
        "role": user["role"],
        "action": action,
        "message": message
    })

@app.route("/last-tap", methods=["GET"])
def last_tap_route():
    return jsonify(last_tap)

@app.route("/users", methods=["GET"])
def users_list():
    return jsonify(db.get_all_users())

@app.route("/users", methods=["POST"])
def users_create():
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip().upper()
    name = str(data.get("name", "")).strip()
    role = str(data.get("role", "")).strip().lower()

    if not uid or not name or role not in ("student", "visitor"):
        return jsonify({"success": False, "message": "Missing or invalid fields"})

    existing = db.uid_exists(uid)
    if existing:
        if existing["is_temp"]:
            db.upgrade_temp_user(uid, name, role)
            return jsonify({"success": True, "message": f"{name} registered ({role}) - temp card upgraded"})
        return jsonify({"success": False, "message": "This card is already registered"})

    db.add_user(uid, name, role)
    return jsonify({"success": True, "message": f"{name} registered as {role}"})

@app.route("/users/<uid>", methods=["DELETE"])
def users_delete(uid):
    db.delete_user(uid.upper())
    return jsonify({"success": True})

@app.route("/logs", methods=["GET"])
def logs_route():
    return jsonify(db.get_recent_logs())

if __name__ == "__main__":
    db.init_db()
    print("Server running! Keep this window open.")
    app.run(host="0.0.0.0", port=5000)
```

---

## 8. FILE 3 — `app_ui.py`

Create this file with EXACTLY this content:

```python
import tkinter as tk
from tkinter import ttk, messagebox
import requests

SERVER = "http://127.0.0.1:5000"

class RfidApp:
    def __init__(self, root):
        self.root = root
        root.title("RFID Attendance System - Admin")
        root.geometry("780x560")
        self.capture_count = None
        self.build_register_frame()
        self.build_tabs()
        self.refresh_users()
        self.poll_capture()

    def api(self, method, path, body=None):
        try:
            return requests.request(method, SERVER + path, json=body, timeout=3).json()
        except requests.exceptions.RequestException:
            messagebox.showerror("Connection error", "Cannot reach server.\nIs app.py running?")
            return None

    def build_register_frame(self):
        box = ttk.LabelFrame(self.root, text=" Register a New Card ")
        box.pack(fill="x", padx=10, pady=8)
        ttk.Label(box, text="UID:").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        self.uid_entry = ttk.Entry(box, width=26)
        self.uid_entry.grid(row=0, column=1, padx=6)
        self.capture_btn = ttk.Button(box, text="Tap card to capture UID", command=self.start_capture)
        self.capture_btn.grid(row=0, column=2, padx=6)
        self.status_lbl = ttk.Label(box, text="", foreground="green")
        self.status_lbl.grid(row=0, column=3, padx=6)
        ttk.Label(box, text="Name:").grid(row=1, column=0, sticky="e", padx=6, pady=6)
        self.name_entry = ttk.Entry(box, width=26)
        self.name_entry.grid(row=1, column=1, padx=6)
        self.role_var = tk.StringVar(value="student")
        ttk.Radiobutton(box, text="Student", variable=self.role_var, value="student").grid(row=1, column=2, sticky="w")
        ttk.Radiobutton(box, text="Visitor", variable=self.role_var, value="visitor").grid(row=1, column=3, sticky="w")
        ttk.Button(box, text="Save to Database", command=self.save_user).grid(row=2, column=1, pady=8)

    def build_tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=8)

        tab1 = ttk.Frame(nb)
        nb.add(tab1, text="Registered Cards")
        cols = ("uid", "name", "role", "type", "registered")
        self.tree = ttk.Treeview(tab1, columns=cols, show="headings", height=12)
        for col, text, width in (("uid", "UID", 150), ("name", "Name", 160), ("role", "Role", 80),
                                 ("type", "Type", 90), ("registered", "Registered At", 150)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width)
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        btns = ttk.Frame(tab1); btns.pack(pady=4)
        ttk.Button(btns, text="Refresh", command=self.refresh_users).pack(side="left", padx=4)
        ttk.Button(btns, text="Delete Selected", command=self.delete_selected).pack(side="left", padx=4)

        tab2 = ttk.Frame(nb)
        nb.add(tab2, text="Recent Taps (In/Out)")
        lcols = ("name", "role", "time_in", "time_out")
        self.log_tree = ttk.Treeview(tab2, columns=lcols, show="headings", height=12)
        for col, text, width in (("name", "Name", 160), ("role", "Role", 90),
                                 ("time_in", "Time In", 160), ("time_out", "Time Out", 160)):
            self.log_tree.heading(col, text=text)
            self.log_tree.column(col, width=width)
        self.log_tree.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(tab2, text="Refresh Logs", command=self.refresh_logs).pack(pady=4)

    def start_capture(self):
        data = self.api("GET", "/last-tap")
        if data is not None:
            self.capture_count = data["count"]
            self.status_lbl.config(text="Now tap a card on the reader...", foreground="orange")

    def poll_capture(self):
        if self.capture_count is not None:
            try:
                data = requests.get(SERVER + "/last-tap", timeout=2).json()
                if data["count"] != self.capture_count:
                    self.uid_entry.delete(0, tk.END)
                    self.uid_entry.insert(0, data["uid"])
                    self.status_lbl.config(text="UID captured!", foreground="green")
                    self.capture_count = None
            except Exception:
                pass
        self.root.after(700, self.poll_capture)

    def save_user(self):
        uid = self.uid_entry.get().strip().upper()
        name = self.name_entry.get().strip()
        if not uid or not name:
            messagebox.showwarning("Missing info", "Capture a card UID and type a name first.")
            return
        data = self.api("POST", "/users", {"uid": uid, "name": name, "role": self.role_var.get()})
        if data and data.get("success"):
            messagebox.showinfo("Saved", data["message"])
            self.uid_entry.delete(0, tk.END)
            self.name_entry.delete(0, tk.END)
            self.status_lbl.config(text="")
            self.refresh_users()
        elif data:
            messagebox.showerror("Not saved", data.get("message", "Unknown error"))

    def refresh_users(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        data = self.api("GET", "/users")
        if data:
            for u in data:
                self.tree.insert("", "end", values=(u["rfid_uid"], u["name"], u["role"],
                    "Temporary" if u["is_temp"] else "Permanent", u["registered_at"]))

    def refresh_logs(self):
        for i in self.log_tree.get_children(): self.tree.delete(i)
        data = self.api("GET", "/logs")
        if data:
            for r in data:
                self.log_tree.insert("", "end", values=(r["name"], r["role"], r["time_in"], r["time_out"] or "STILL IN"))

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("No selection", "Click a row in the list first.")
            return
        uid = self.tree.item(sel[0], "values")[0]
        if messagebox.askyesno("Confirm", f"Delete card {uid}?"):
            self.api("DELETE", f"/users/{uid}")
            self.refresh_users()

if __name__ == "__main__":
    root = tk.Tk()
    RfidApp(root)
    root.mainloop()
```

---

## 9. FILE 4 — `rfid_reader/rfid_reader.ino` (ESP32 Sketch)

Create folder `rfid_reader` and inside it the file `rfid_reader.ino` with EXACTLY this content:

```cpp
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ============ CHANGE THESE 3 LINES ============
const char* WIFI_SSID  = "YOUR_WIFI_NAME";
const char* WIFI_PASS  = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL = "http://192.168.1.50:5000/tap";  // your PC's IP address
// ==============================================

#define SS_PIN  5
#define RST_PIN 4
#define I2C_SDA 21
#define I2C_SCL 22

MFRC522 rfid(SS_PIN, RST_PIN);
LiquidCrystal_I2C lcd(0x27, 16, 2);   // if LCD stays blank, try 0x3F instead of 0x27

String uidToHex(MFRC522& reader) {
  String uid = "";
  for (byte i = 0; i < reader.rfid.uid.size; i++) {
    if (reader.rfid.uid.uidByte[i] < 0x10) uid += "0";  // zero-pad so UIDs always match the DB
    uid += String(reader.rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  return uid;
}

void lcdMessage(String line1, String line2) {
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print(line1.substring(0, 16));
  lcd.setCursor(0, 1); lcd.print(line2.substring(0, 16));
}

void connectWiFi() {
  lcdMessage("Connecting WiFi", "Please wait...");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.println("\nWiFi OK! ESP32 IP: " + WiFi.localIP().toString());
}

void setup() {
  Serial.begin(115200);
  SPI.begin();
  rfid.PCD_Init();
  Wire.begin(I2C_SDA, I2C_SCL);
  lcd.init();
  lcd.backlight();
  connectWiFi();
  lcdMessage("System Ready", "Tap your card");
}

void loop() {
  if (!rfid.PICC_IsNewCardPresent()) return;
  if (!rfid.PICC_ReadCardSerial()) return;

  String uid = uidToHex(rfid);
  Serial.println("Card UID: " + uid);

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();

  if (WiFi.status() != WL_CONNECTED) connectWiFi();

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");

  String body = "{\"uid\":\"" + uid + "\"}";
  int httpCode = http.POST(body);

  if (httpCode == 200) {
    JsonDocument doc;   // ArduinoJson v7. If you have v6, use: StaticJsonDocument<512> doc;
    if (deserializeJson(doc, http.getString()) == DeserializationError::Ok) {
      String status  = doc["status"] | "";
      String name    = doc["name"] | "";
      String action  = doc["action"] | "";
      if (status == "granted")      lcdMessage(name, action + " - OK");
      else                          lcdMessage("ACCESS DENIED", "Not registered");
    }
  } else {
    Serial.println("HTTP error: " + String(httpCode));
    lcdMessage("SERVER ERROR", "Code: " + String(httpCode));
  }

  http.end();
  delay(2000);                       // show result + anti-spam cooldown
  lcdMessage("System Ready", "Tap your card");
}
```

---

## 10. Configuration Steps (Agent Must Do These)

1. **Find the PC's IP address:**
   - Windows: run `ipconfig` → note the **IPv4 Address** (e.g., `192.168.1.50`).
    - Put it into `SERVER_URL` in the ESP32 sketch: `http://<PC_IP>:5001/tap`
    - The PC address can change; recheck it with `ipconfig` before hardware tests.
2. **WiFi:** Put the WiFi name and password into the sketch.
   - PC and ESP32 **must be on the same network**.
   - ESP32 only supports **2.4 GHz** WiFi (not 5 GHz).
   - **Recommended for demo:** use a **phone hotspot** — connect both the PC and ESP32 to it.
     School/office networks often block device-to-device communication.
3. **LCD address:** default is `0x27`. If the LCD stays blank, change to `0x3F`.

---

## 11. Execution & Test Plan

### Phase A — Backend test (NO hardware needed)

1. Run the isolated API tests:
    ```bash
    python -m unittest test_server
    ```
2. Start the server in one terminal:
    ```bash
    python app.py
    ```
    The Flask server listens on port 5001. Keep this terminal running.
3. In another terminal, run the read-only live check:
    ```bash
    python test_server.py
    ```
    A real tap changes attendance data; submit one only with `python test_server.py --tap-uid UID`.

### Phase B — UI test

4. In separate terminals run:
    ```bash
    python app_ui.py
    python student_display.py
    ```
    The admin window manages cards and flagged attendance. The student display shows status and results; attendance mode is automatic.

### Phase C — Hardware test

5. Confirm the PC's current IPv4 address with `ipconfig` and update `SERVER_URL` in the sketch to `http://<PC_IP>:5001/tap`.
6. Upload `rfid_reader.ino` to the ESP32 and open Serial Monitor at **115200 baud**. Confirm WiFi reports a connected ESP32 IP.
   The LCD shows the scan summary and then the role/message for about 4.5 seconds, matching the student display's result duration.
7. Start Capture UID in the admin UI and tap the physical card. The next reader scan is captured once, shows `CARD READ SUCCESSFULLY`, and is not recorded as attendance. Complete the form and save the card.
8. Tap a registered card; it should show TIME IN. A tap within 30 seconds should show the remaining cooldown and leave attendance unchanged.
9. After 30 seconds, tap again; it should show TIME OUT. After another 30 seconds, the next tap should show TIME IN.
10. An unknown card should be denied and should not be added automatically. Deleting a card archives it and retains its attendance history; the UID can then be captured and registered again as a separate record. Name corrections in the admin UI must preserve the card, student number, and attendance history.

---

## 12. Acceptance Checklist

- [ ] `python app.py` starts without errors; `rfid_system.db` is created.
- [ ] `python -m unittest test_server` passes.
- [ ] The read-only `python test_server.py` check reaches the server.
- [ ] `python app_ui.py` opens; users and logs tabs display data.
- [ ] `python student_display.py` opens without an attendance control.
- [ ] ESP32 connects to WiFi (Serial Monitor confirms).
- [ ] A scan records IN when no record is open; scans within 30 seconds are rejected without changing the log.
- [ ] After cooldown, a tap automatically records OUT; after another cooldown, a tap records a new IN.
- [ ] Unknown cards are denied; visitor overstay/missing-time-out flags appear in the UI.

---

## 13. Troubleshooting Reference

| Symptom | Likely Cause | Fix |
|---|---|---|
| LCD completely blank | Contrast or wrong I2C address | Turn the pot on the LCD back; change `0x27` → `0x3F` in sketch |
| RC522 never reads cards | Powered from 5V, or bad SPI wiring | Use **3V3** only; recheck SPI pin table |
| ESP32: `SERVER ERROR` / code -1 | Server not running, wrong IP, or firewall | Start `app.py`; verify IP with `ipconfig`; allow Python through firewall |
| ESP32 stuck on "Connecting WiFi" | Wrong password or 5 GHz network | Use 2.4 GHz / phone hotspot; recheck password |
| Works on hotspot but not school WiFi | Network isolation (client isolation) | Demo using a phone hotspot for both PC and ESP32 |
| Compile error on `JsonDocument doc;` | ArduinoJson v6 installed | Replace with `StaticJsonDocument<512> doc;` or update the library |
| Upload fails / hangs | Wrong port or boot mode | Hold **BOOT** button during "Connecting..."; check correct COM port |
| UID mismatch between card and DB | UID formatting altered | Never edit UID text manually; always capture via UI button |

---

## 14. Important Notes

- **Do not** connect RC522 VCC to 5V — 3.3V only.
- The Flask server must stay running (`python app.py`) for the whole system to work.
- The ESP32 needs access to the PC on the same reachable LAN; public internet access is not required.
- The Flask API has no authentication and listens on the LAN. Use only a trusted network and restrict port 5001 with the firewall.
- WiFi credentials are currently stored in the sketch. Rotate any credential that has been shared and avoid committing real credentials.
- The database file `rfid_system.db` lives in the same folder as `app.py`.
  Delete it to reset the system to a clean state.
- All UIDs are stored **uppercase with zero-padded hex bytes** — this matching logic
  is already handled in both the ESP32 code and the server. Do not modify it.
