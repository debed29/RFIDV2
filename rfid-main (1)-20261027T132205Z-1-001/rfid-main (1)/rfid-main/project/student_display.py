"""Public-facing RFID kiosk display. Run alongside app.py and app_ui.py.

Keys:  T = toggle dark / light theme     F11 = fullscreen     Esc = leave fullscreen
"""
import json
import math
import os
import random
import sys
import threading
import time

import requests
from PySide6.QtCore import QLineF, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget

SERVER = os.environ.get("RFID_SERVER", "http://127.0.0.1:5001")
DISPLAY_SECONDS = 4.5         # how long a result stays on screen
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "student_display_settings.json")
FONTS = ["Segoe UI", "Inter", "Helvetica Neue", "Arial"]


def C(hex_color, alpha=1.0):
    color = QColor(hex_color)
    color.setAlphaF(alpha)
    return color


# Light theme is deliberately low-glare: nothing brighter than a soft slate-grey, strong dark text.
PALETTES = {
    "dark": {
        "bg0": C("#070E1A"), "bg1": C("#10203A"), "orb": C("#0F1D33"), "card": C("#13233B"),
        "text": C("#EAF3FA"), "muted": C("#8FA7BB"),
        "accent": C("#38D0F0"), "green": C("#3DDC97"), "red": C("#FF6B7F"), "amber": C("#F5B94A"),
        "grid": QColor(130, 170, 210, 16), "blob": 34, "glow": 80,
    },
    "light": {
        "bg0": C("#B4BED2"), "bg1": C("#CAD2E1"), "orb": C("#DCE2EE"), "card": C("#D6DDEA"),
        "text": C("#18233D"), "muted": C("#47556F"),
        "accent": C("#0A7690"), "green": C("#17784F"), "red": C("#B03048"), "amber": C("#8F5700"),
        "grid": QColor(40, 60, 100, 20), "blob": 26, "glow": 46,
    },
}

# ---- easing --------------------------------------------------------------------------
def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def ease_out_cubic(x):
    return 1 - (1 - clamp(x)) ** 3


def ease_out_back(x):
    x = clamp(x)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def lerp(a, b, k):
    return a + (b - a) * k


# ---- network (runs off the UI thread so animations never stutter) ------------------------
class Bridge(QObject):
    event = Signal(dict)
    online = Signal(bool)


class StudentDisplay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RFID Attendance | Student Display")
        self.resize(1100, 760)
        self.setMinimumSize(800, 600)

        self.theme_mode = self.load_theme()
        self.state = "idle"          # idle | granted | warning | denied
        self.name = self.action = self.role = ""
        self.message = ""
        self.result = ""
        self.t0 = time.monotonic()
        self.state_start = self.t0
        self.server_online = None    # None = unknown yet
        self.particles = []
        self.bg_cache = None

        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event)
        self.bridge.online.connect(self.on_online)
        self.running = True
        threading.Thread(target=self.poll_loop, daemon=True).start()

        self.apply_theme()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    # ---- settings / theme -------------------------------------------------------------
    def load_theme(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
                mode = json.load(handle).get("theme")
                if mode in PALETTES:
                    return mode
        except (OSError, ValueError):
            pass
        return "dark"

    def save_theme(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
                json.dump({"theme": self.theme_mode}, handle)
        except OSError:
            pass

    def apply_theme(self):
        self.bg_cache = None
        self.update()

    def toggle_theme(self):
        self.theme_mode = "light" if self.theme_mode == "dark" else "dark"
        self.save_theme()
        self.apply_theme()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_T:
            self.toggle_theme()
        elif key == Qt.Key.Key_F11:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif key == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.running = False
        super().closeEvent(event)

    # ---- server communication --------------------------------------------------------
    def poll_loop(self):
        last_id = None
        while self.running:
            try:
                response = requests.get(SERVER + "/display-event", timeout=1.2)
                response.raise_for_status()
                event = response.json()
                self.bridge.online.emit(True)
                event_id = int(event.get("event_id", 0))
                if event_id != last_id:
                    # The first poll only records the current id, so an old result is not replayed at launch.
                    if last_id is not None and event_id > 0:
                        self.bridge.event.emit(event)
                    last_id = event_id
            except (requests.RequestException, ValueError, TypeError):
                self.bridge.online.emit(False)
            time.sleep(0.35)

    def on_online(self, online):
        self.server_online = online

    def on_event(self, event):
        self.result = event.get("result", "")
        self.set_state(event.get("status", "idle"), event.get("name", ""), event.get("action", ""),
                       event.get("role", ""), event.get("message", ""))

    def set_state(self, state, name="", action="", role="", message=""):
        self.state = state
        self.name, self.action, self.role, self.message = name, action, role, message
        self.state_start = time.monotonic()
        rng = random.Random(self.state_start)
        self.particles = []
        if state in ("granted", "capture_success", "registration_success"):
            for _ in range(26):
                self.particles.append((rng.uniform(0, math.tau), rng.uniform(150, 330),
                                       rng.uniform(3, 7), rng.uniform(0.7, 1.2), rng.uniform(0.05, 0.25)))

    # ---- frame loop -----------------------------------------------------------------------
    def tick(self):
        now = time.monotonic()
        if self.state != "idle" and now - self.state_start > DISPLAY_SECONDS:
            self.set_state("idle")
        self.update()

    def scale_factor(self):
        return clamp(min(self.width() / 1100, self.height() / 760), 0.6, 1.9)

    def resizeEvent(self, event):
        self.bg_cache = None
        super().resizeEvent(event)

    # ---- drawing helpers ------------------------------------------------------------------
    def text(self, p, content, cx, cy, px, color, bold=False, spacing=0.0, alpha=1.0, max_w=None, dy=0.0):
        font = QFont()
        font.setFamilies(FONTS)
        font.setPixelSize(max(8, int(px)))
        font.setBold(bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
        metrics = QFontMetrics(font)
        if max_w:
            content = metrics.elidedText(content, Qt.TextElideMode.ElideRight, int(max_w))
        pen = QColor(color)
        pen.setAlphaF(clamp(pen.alphaF() * alpha))
        p.setFont(font)
        p.setPen(pen)
        p.drawText(QRectF(cx - 1400, cy - px + dy, 2800, px * 2), Qt.AlignmentFlag.AlignCenter, content)
        return metrics.horizontalAdvance(content)

    def background(self, p, P, w, h, t, s):
        if self.bg_cache is None:
            ratio = self.devicePixelRatio()
            cache = QPixmap(int(w * ratio), int(h * ratio))
            cache.setDevicePixelRatio(ratio)
            cp = QPainter(cache)
            cp.setRenderHint(QPainter.RenderHint.Antialiasing)
            gradient = QRadialGradient(w * 0.5, h * 0.42, max(w, h) * 0.75)
            gradient.setColorAt(0, P["bg1"])
            gradient.setColorAt(1, P["bg0"])
            cp.fillRect(0, 0, w, h, gradient)
            cp.setPen(Qt.PenStyle.NoPen)
            cp.setBrush(P["grid"])
            step = max(30, int(46 * s))
            for x in range(step // 2, w, step):
                for y in range(step // 2, h, step):
                    cp.drawEllipse(QRectF(x - 1.2, y - 1.2, 2.4, 2.4))
            cp.end()
            self.bg_cache = cache
        p.drawPixmap(0, 0, self.bg_cache)
        # slow drifting colour blobs give the background gentle life
        for index, key in enumerate(("accent", "green", "accent")):
            x = w * (0.2 + 0.3 * index) + math.sin(t * 0.17 + index * 2.1) * w * 0.07
            y = h * (0.35 + 0.25 * (index % 2)) + math.cos(t * 0.13 + index) * h * 0.06
            blob = QRadialGradient(x, y, min(w, h) * 0.55)
            blob.setColorAt(0, QColor(P[key].red(), P[key].green(), P[key].blue(), P["blob"]))
            blob.setColorAt(1, QColor(P[key].red(), P[key].green(), P[key].blue(), 0))
            p.fillRect(0, 0, w, h, blob)

    def top_bar(self, p, P, w, s):
        margin = 36 * s
        size = 34 * s
        rect = QRectF(margin, 26 * s, size, size)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(P["accent"].red(), P["accent"].green(), P["accent"].blue(), 40))
        p.drawRoundedRect(rect, 10 * s, 10 * s)
        self.text(p, "R", rect.center().x(), rect.center().y(), 19 * s, P["accent"], True)
        font = QFont()
        font.setFamilies(FONTS)
        font.setPixelSize(int(14 * s))
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.5)
        p.setFont(font)
        p.setPen(P["muted"])
        p.drawText(QRectF(margin + size + 12 * s, 26 * s, 400, size), Qt.AlignmentFlag.AlignVCenter, "RFID ATTENDANCE")

        local = time.localtime()
        hour = local.tm_hour % 12 or 12
        clock = f"{hour}:{local.tm_min:02d} {'AM' if local.tm_hour < 12 else 'PM'}"
        date = f"{time.strftime('%A, %B')} {local.tm_mday}"
        right = w - margin
        font.setPixelSize(int(26 * s))
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0)
        p.setFont(font)
        p.setPen(P["text"])
        p.drawText(QRectF(right - 400, 20 * s, 400, 32 * s), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, clock)
        font.setBold(False)
        font.setPixelSize(int(13 * s))
        p.setFont(font)
        p.setPen(P["muted"])
        p.drawText(QRectF(right - 400, 52 * s, 400, 20 * s), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, date)

        if self.server_online is False:
            blink = 0.55 + 0.45 * math.sin((time.monotonic() - self.t0) * 4)
            label = "Server offline - reconnecting"
            width = QFontMetrics(font).horizontalAdvance(label) + 60 * s
            pill = QRectF((w - width) / 2, 28 * s, width, 32 * s)
            p.setPen(QPen(C(P["red"].name(), 0.7), 1.2))
            p.setBrush(C(P["red"].name(), 0.14))
            p.drawRoundedRect(pill, 16 * s, 16 * s)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(C(P["red"].name(), blink))
            p.drawEllipse(QRectF(pill.left() + 14 * s, pill.center().y() - 4 * s, 8 * s, 8 * s))
            p.setPen(P["text"])
            p.drawText(pill.adjusted(28 * s, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, label)

    @staticmethod
    def line(p, x1, y1, x2, y2):
        p.drawLine(QLineF(x1, y1, x2, y2))

    def glow_disc(self, p, color, cx, cy, radius, strength):
        glow = QRadialGradient(cx, cy, radius)
        glow.setColorAt(0, QColor(color.red(), color.green(), color.blue(), int(strength)))
        glow.setColorAt(1, QColor(color.red(), color.green(), color.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))

    def orb(self, p, P, color, cx, cy, r, s, ring_width=3.0):
        p.setPen(Qt.PenStyle.NoPen)
        fill = QLinearGradient(cx, cy - r, cx, cy + r)
        fill.setColorAt(0, P["card"])
        fill.setColorAt(1, P["orb"])
        p.setBrush(fill)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
        p.setPen(QPen(color, ring_width * s))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

    # ---- screens ----------------------------------------------------------------------------
    def draw_idle(self, p, P, w, h, t, s, fade):
        color = P["accent"]
        cx, cy = w / 2, h * 0.40
        r = 100 * s * (1 + 0.02 * math.sin(t * 1.8))

        p.setOpacity(fade)
        for i in range(3):
            phase = ((t / 3.6) + i / 3) % 1
            rr = r + ease_out_cubic(phase) * 120 * s
            alpha = (1 - phase) ** 1.5 * (0.55 if self.theme_mode == "dark" else 0.5)
            p.setPen(QPen(C(color.name(), alpha), 2 * s))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2, rr * 2))

        self.glow_disc(p, color, cx, cy, r * 2.0, P["glow"])
        self.orb(p, P, color, cx, cy, r, s)

        # card glyph with "contactless" waves that pulse upward
        bob = math.sin(t * 2.2) * 4 * s
        p.setPen(QPen(color, 3.2 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        card = QRectF(cx - 42 * s, cy - 14 * s + bob, 84 * s, 54 * s)
        p.drawRoundedRect(card, 10 * s, 10 * s)
        self.line(p, card.left() + 14 * s, card.top() + 38 * s, card.left() + 40 * s, card.top() + 38 * s)
        p.setBrush(C(color.name(), 0.85))
        p.drawRoundedRect(QRectF(card.left() + 14 * s, card.top() + 12 * s, 16 * s, 12 * s), 3 * s, 3 * s)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(3):
            rad = (14 + i * 11) * s
            wave = clamp(math.sin(t * 3.4 - i * 0.9) * 0.5 + 0.5)
            p.setPen(QPen(C(color.name(), 0.2 + 0.8 * wave), 3.2 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(cx - rad, cy - 22 * s + bob - rad, rad * 2, rad * 2), 55 * 16, 70 * 16)

        # message
        if self.server_online is False:
            title, sub, tint = "Scanning unavailable", "Please see the attendant", P["red"]
        else:
            title, sub, tint = "Ready to scan", "Hold your card near the reader", P["text"]
        pulse = 0.85 + 0.15 * math.sin(t * 2.0)
        self.text(p, title, cx, cy + 232 * s, 42 * s, tint, True, alpha=pulse, max_w=w * 0.9)
        self.text(p, sub, cx, cy + 282 * s, 19 * s, P["muted"], False, 0.4, max_w=w * 0.9)
        p.setOpacity(1)

    def draw_result(self, p, P, w, h, t, s):
        e = time.monotonic() - self.state_start
        out = clamp((DISPLAY_SECONDS - e) / 0.4)  # fade out at the end
        color = {
            "granted": P["green"],
            "capture_success": P["green"],
            "registration_success": P["green"],
            "warning": P["amber"],
            "card_already_registered": P["red"],
            "capture_failed": P["amber"],
        }.get(self.state, P["red"])
        cx, cy = w / 2, h * 0.37

        # colour flash across the screen at the start
        if e < 0.7:
            flash = QRadialGradient(cx, cy, max(w, h) * 0.8)
            flash.setColorAt(0, QColor(color.red(), color.green(), color.blue(), int(70 * (1 - e / 0.7))))
            flash.setColorAt(1, QColor(color.red(), color.green(), color.blue(), 0))
            p.fillRect(0, 0, w, h, flash)

        p.setOpacity(out)
        pop = lerp(0.55, 1.0, ease_out_back(e / 0.55))
        r = 100 * s * pop
        shake = math.sin(e * 46) * 14 * s * math.exp(-e * 7) if self.state == "denied" else 0.0
        ox = cx + shake

        self.glow_disc(p, color, ox, cy, r * (2.1 + 0.08 * math.sin(t * 3)), P["glow"] * 1.5)
        for i in range(2):  # expanding shock rings
            k = clamp((e - i * 0.18) / 0.9)
            if 0 < k < 1:
                rr = r * (1 + 0.9 * ease_out_cubic(k))
                p.setPen(QPen(C(color.name(), 0.6 * (1 - k)), 3 * s))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QRectF(ox - rr, cy - rr, rr * 2, rr * 2))
        self.orb(p, P, color, ox, cy, r, s, 4.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(C(color.name(), 0.14))
        p.drawEllipse(QRectF(ox - r, cy - r, r * 2, r * 2))

        # animated icon
        draw = clamp((e - 0.12) / 0.5)
        p.setPen(QPen(color, 7 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        k = 1.25 * s * pop
        if self.state in ("granted", "capture_success", "registration_success"):
            a, b, c = (-30, 2), (-8, 24), (36, -24)
            seg1, seg2 = 31.0, 65.0
            dist = ease_out_cubic(draw) * (seg1 + seg2)
            first = min(dist, seg1) / seg1
            self.line(p, ox + a[0] * k, cy + a[1] * k, ox + lerp(a[0], b[0], first) * k, cy + lerp(a[1], b[1], first) * k)
            if dist > seg1:
                second = (dist - seg1) / seg2
                self.line(p, ox + b[0] * k, cy + b[1] * k, ox + lerp(b[0], c[0], second) * k, cy + lerp(b[1], c[1], second) * k)
        elif self.state in ("warning", "capture_failed"):
            length = ease_out_cubic(draw)
            self.line(p, ox, cy - 36 * k, ox, cy - 36 * k + 42 * k * length)
            if draw > 0.8:
                p.setBrush(color)
                dot = 4.5 * k * ease_out_back((draw - 0.8) / 0.2)
                p.drawEllipse(QRectF(ox - dot, cy + 24 * k - dot, dot * 2, dot * 2))
        else:
            first = clamp(draw * 2)
            second = clamp(draw * 2 - 1)
            self.line(p, ox - 26 * k, cy - 26 * k, ox - 26 * k + 52 * k * first, cy - 26 * k + 52 * k * first)
            self.line(p, ox + 26 * k, cy - 26 * k, ox + 26 * k - 52 * k * second, cy - 26 * k + 52 * k * second)

        # celebration particles
        p.setPen(Qt.PenStyle.NoPen)
        for angle, speed, size, life, delay in self.particles:
            k2 = (e - delay) / life
            if 0 < k2 < 1:
                dist = r * 1.05 + speed * s * ease_out_cubic(k2) * 0.9
                px, py = ox + math.cos(angle) * dist, cy + math.sin(angle) * dist + 28 * s * k2 * k2
                p.setBrush(C(color.name(), 1 - k2))
                p.drawEllipse(QRectF(px - size * s, py - size * s, size * 2 * s, size * 2 * s))

        # staggered text
        def reveal(delay):
            k3 = ease_out_cubic((e - delay) / 0.4)
            return k3, (1 - k3) * 16 * s

        title = {"granted": "TIME OUT RECORDED" if self.action == "OUT" else "TIME IN RECORDED",
                 "capture_success": "CARD READ SUCCESSFULLY",
                 "registration_success": "SUCCESSFULLY REGISTERED",
                 "card_already_registered": "CARD ALREADY REGISTERED",
                 "capture_failed": "INVALID CARD UID",
                 "warning": "PLEASE WAIT" if self.result == "REJECT_COOLDOWN"
                 else "ALREADY CHECKED IN"}.get(self.state, "ACCESS DENIED")
        if self.state == "capture_success":
            a, dy = reveal(0.18)
            button_font = QFont()
            button_font.setFamilies(FONTS)
            button_font.setPixelSize(int(20 * s))
            button_font.setBold(True)
            button_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6 * s)
            button_width = min(
                w * 0.88, QFontMetrics(button_font).horizontalAdvance(title) + 48 * s
            )
            button_rect = QRectF(cx - button_width / 2, cy + 132 * s + dy, button_width, 48 * s)
            p.setPen(QPen(C(color.name(), 0.9 * a), 1.6 * s))
            p.setBrush(C(color.name(), 0.2 * a))
            p.drawRoundedRect(button_rect, 18 * s, 18 * s)
            p.setFont(button_font)
            p.setPen(C(color.name(), a))
            p.drawText(button_rect, Qt.AlignmentFlag.AlignCenter, title)
            self.text(p, "Ready to complete registration", cx, cy + 218 * s,
                      18 * s, P["muted"], False, 0.2, a, w * 0.9)
            name_y = 222
        elif self.state == "granted":
            a, dy = reveal(0.18)
            self.text(p, title, cx, cy + 162 * s, 34 * s, color, True, 2.0, a, w * 0.9, dy)
            fallback = "Welcome"
            a, dy = reveal(0.30)
            self.text(p, self.name or fallback, cx, cy + 222 * s, 46 * s, P["text"], True, 0, a, w * 0.9, dy)
            name_y = 222
        else:
            a, dy = reveal(0.18)
            self.text(p, title, cx, cy + 162 * s, 34 * s, color, True, 2.0, a, w * 0.9, dy)
            fallback = "" if self.state in (
                "warning", "card_already_registered", "capture_failed"
            ) else "Unknown card"
            a, dy = reveal(0.30)
            self.text(p, self.name or fallback, cx, cy + 222 * s, 46 * s, P["text"], True, 0, a, w * 0.9, dy)
            name_y = 222

        # Overstay alert badge for visitors who exceeded their time limit
        a_badge, dy_badge = reveal(0.36)
        has_overstay_badge = False
        if self.result == "OVERSTAY" and self.role == "visitor":
            has_overstay_badge = True
            badge_text = "⚠ OVERSTAY"
            badge_font = QFont()
            badge_font.setFamilies(FONTS)
            badge_font.setPixelSize(int(16 * s))
            badge_font.setBold(True)
            badge_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
            badge_width = QFontMetrics(badge_font).horizontalAdvance(badge_text) + 36 * s
            badge_rect = QRectF(cx - badge_width / 2, cy + (name_y + 40) * s + dy_badge, badge_width, 30 * s)
            p.setPen(QPen(C(P["red"].name(), 0.8 * a_badge), 1.5))
            p.setBrush(C(P["red"].name(), 0.18 * a_badge))
            p.drawRoundedRect(badge_rect, 15 * s, 15 * s)
            p.setPen(P["red"])
            p.setFont(badge_font)
            p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)

        a, dy = reveal(0.42)
        # Shift role chip down if overstay badge is shown to avoid overlap
        role_y_offset = 42 if has_overstay_badge else 0
        content_y = name_y + 40
        if self.role:
            label = self.role.upper()
            font = QFont()
            font.setFamilies(FONTS)
            font.setPixelSize(int(14 * s))
            font.setBold(True)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
            width = QFontMetrics(font).horizontalAdvance(label) + 44 * s
            chip = QRectF(cx - width / 2, cy + content_y * s + dy + role_y_offset, width, 32 * s)
            p.setPen(QPen(C(color.name(), 0.7 * a), 1.4))
            p.setBrush(C(color.name(), 0.16 * a))
            p.drawRoundedRect(chip, 16 * s, 16 * s)
            self.text(p, label, cx, chip.center().y(), 14 * s, color, True, 2.0, a)
        a, dy = reveal(0.54)
        # Shift message down if overstay badge is shown
        message_y_offset = 42 if has_overstay_badge else 0
        self.text(p, self.message, cx, cy + (content_y + 64) * s + message_y_offset, 19 * s, P["muted"], False, 0.2, a, w * 0.85, dy)

        # countdown bar back to the idle screen
        bar_w = min(w * 0.4, 420 * s)
        bar = QRectF((w - bar_w) / 2, h - 52 * s, bar_w, 6 * s)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(C(P["muted"].name(), 0.25))
        p.drawRoundedRect(bar, 3 * s, 3 * s)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(bar.left(), bar.top(), bar_w * clamp(1 - e / DISPLAY_SECONDS), bar.height()), 3 * s, 3 * s)
        p.setOpacity(1)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        P = PALETTES[self.theme_mode]
        w, h = self.width(), self.height()
        s = self.scale_factor()
        now = time.monotonic()
        t = now - self.t0

        self.background(p, P, w, h, t, s)
        self.top_bar(p, P, w, s)
        if self.state == "idle":
            self.draw_idle(p, P, w, h, t, s, ease_out_cubic((now - self.state_start) / 0.6))
        else:
            self.draw_result(p, P, w, h, t, s)

        if self.state == "idle":
            self.text(p, "T  theme     F11  fullscreen", w - 150 * s, h - 24 * s, 12 * s, P["muted"], False, 0.5, 0.55)
        p.end()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = StudentDisplay()
    window.showMaximized()
    sys.exit(app.exec())