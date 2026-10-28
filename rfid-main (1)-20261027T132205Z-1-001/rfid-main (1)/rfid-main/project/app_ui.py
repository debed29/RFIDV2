import csv
import json
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import requests


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "ui_settings.json")
DEFAULT_SETTINGS = {"server": "http://127.0.0.1:5001", "theme": "dark", "page_size": 8}


def format_name_for_display(record):
    surname = str(record.get("surname") or "").strip()
    first_name = str(record.get("first_name") or "").strip()
    middle_name = str(record.get("middle_name") or "").strip()
    if surname and first_name:
        return " ".join(part for part in (surname, first_name, middle_name) if part)
    return str(record.get("name") or "")

PAGES = ("Dashboard", "Register Card", "Registered Cards", "Recent Taps", "Reports", "Settings")
PAGE_META = {
    "Dashboard": ("Dashboard", "Live overview of cards and today's attendance"),
    "Register Card": ("Register Card", "Capture a card UID and register the person"),
    "Registered Cards": ("Registered Cards", "Every card that is registered in the system"),
    "Recent Taps": ("Recent Taps", "Time in / time out records from the reader"),
    "Reports": ("Reports", "Attendance breakdown and CSV exports"),
    "Settings": ("Settings", "Server connection, table size and appearance"),
}

THEMES = {
    "dark": {
        "bg": "#05070B", "sidebar": "#080B11", "panel": "#0B1018", "panel_2": "#0E141E",
        "border": "#202B39", "ctrl_border": "#202B39",
        "text": "#F3F7FA", "muted": "#7F8B99", "label": "#B8C2CD",
        "blue": "#4D9FFF", "cyan": "#54E0D0", "green": "#63E6A8", "red": "#E86B78",
        "amber": "#E9B96E", "purple": "#B7A2E5",
        "entry": "#080C12", "hover": "#17212D", "sep": "#18212C",
        "nav_text": "#AEB8C4", "nav_hover": "#121B27", "nav_active": "#0E141E",
        "primary": "#176D73", "primary_hover": "#20858B", "primary_text": "#F3F7FA",
        "primary_border": "#176D73",
        "danger_fg": "#261317", "danger_hover": "#3A1C22", "danger_border": "#4A252D",
        "scroll": "#273341", "scroll_hover": "#3A4A5B",
        "avatar": "#1A2632", "logo_bg": "#10252A", "accent": "#4D9FFF", "accent_hover": "#347EC4",
        "glow": "#4D9FFF",
    },
    "light": {
        "bg": "#DCD5F2", "sidebar": "#EDE8FC", "panel": "#F7F4FF", "panel_2": "#EDE7FB",
        "border": "#FFFFFF", "ctrl_border": "#D6CAF3",
        "text": "#22173F", "muted": "#6E6790", "label": "#4B4170",
        "blue": "#5B7CFA", "cyan": "#7A5CF0", "green": "#2FA37A", "red": "#D4546A",
        "amber": "#CC8A2E", "purple": "#8B5CF6",
        "entry": "#FDFCFF", "hover": "#E2D7FF", "sep": "#E4DCF7",
        "nav_text": "#4B4170", "nav_hover": "#E2D7FF", "nav_active": "#DCCFFF",
        "primary": "#BBA6FB", "primary_hover": "#D3C4FF", "primary_text": "#2A1A63",
        "primary_border": "#A98BFF",
        "danger_fg": "#FCEBEF", "danger_hover": "#F9D3DB", "danger_border": "#F0BCC6",
        "scroll": "#CFC3F0", "scroll_hover": "#B7A2F0",
        "avatar": "#E2D7FF", "logo_bg": "#E2D7FF", "accent": "#8B6CF0", "accent_hover": "#B9A3FF",
        "glow": "#A98BFF",
    },
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            settings.update(json.load(handle))
    except (OSError, ValueError):
        pass
    if settings.get("theme") not in THEMES:
        settings["theme"] = "dark"
    return settings


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# Reusable table (used by the "Registered Cards" page and the "Recent Taps" page)
# --------------------------------------------------------------------------------------
class DataTable:
    def __init__(self, app, parent, kind):
        self.app = app
        self.kind = kind  # "users" or "logs"
        self.page = 0
        self.selected = tk.StringVar(value="")
        t = app.t

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=15, pady=(14, 9))
        toolbar.grid_columnconfigure(0, weight=1)

        title = "Registered Cards" if kind == "users" else "Recent Taps (In/Out)"
        ctk.CTkLabel(toolbar, text=title, text_color=t["text"],
                     font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w", padx=4)

        self.filter_var = tk.StringVar(value="All")
        values = ["All", "Students", "Visitors"] if kind == "users" else \
                 ["All", "Still in", "Missing time out", "Overstay"]
        app.menu(toolbar, values, self.filter_var, lambda _v: self.on_filter(), width=160)\
            .grid(row=0, column=1, padx=(8, 0))

        self.search = app.entry(toolbar, "Search cards" if kind == "users" else "Search taps",
                                width=220, height=34)
        self.search.grid(row=0, column=2, padx=8)
        self.search.bind("<KeyRelease>", self.on_filter)
        app.btn(toolbar, "Refresh", command=app.refresh_all, width=80, height=34).grid(row=0, column=3)

        self.table = ctk.CTkScrollableFrame(parent, fg_color="transparent",
                                            scrollbar_button_color=t["scroll"],
                                            scrollbar_button_hover_color=t["scroll_hover"])
        self.table.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 5))

        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=17, pady=(3, 13))
        footer.grid_columnconfigure(0, weight=1)
        self.result_count = ctk.CTkLabel(footer, text="", text_color=t["muted"], font=ctk.CTkFont(size=10))
        self.result_count.grid(row=0, column=0, sticky="w")

        self.delete_button = None
        self.edit_button = None
        if kind == "users":
            self.edit_button = app.btn(footer, "Edit student", command=self.edit_selected,
                                       width=112, height=32)
            self.edit_button.grid(row=0, column=1, padx=(0, 8))
            self.edit_button.configure(state="disabled")
            self.delete_button = app.btn(footer, "Delete selected", command=self.delete_selected,
                                         kind="danger", width=132, height=32)
            self.delete_button.grid(row=0, column=2, padx=(0, 12))
            self.delete_button.configure(state="disabled")
            page_column = 3
        else:
            page_column = 1
        self.previous_button = app.btn(footer, "Previous", command=lambda: self.change_page(-1),
                                       kind="ghost", width=78, height=30)
        self.previous_button.grid(row=0, column=page_column, padx=(16, 3) if kind == "users" else 3)
        self.page_label = ctk.CTkLabel(footer, text="1 / 1", width=55, text_color=t["muted"],
                                       font=ctk.CTkFont(size=10))
        self.page_label.grid(row=0, column=page_column + 1, padx=3)
        self.next_button = app.btn(footer, "Next", command=lambda: self.change_page(1),
                                   kind="ghost", width=60, height=30)
        self.next_button.grid(row=0, column=page_column + 2, padx=3)

    # ---- filtering -------------------------------------------------------------------
    def on_filter(self, _event=None):
        self.page = 0
        self.render()

    def change_page(self, delta):
        self.page += delta
        self.render()

    def rows(self):
        app = self.app
        query = self.search.get().strip().casefold()
        choice = self.filter_var.get()
        if self.kind == "users":
            rows = app.users
            if choice == "Students":
                rows = [r for r in rows if r.get("role") == "student"]
            elif choice == "Visitors":
                rows = [r for r in rows if r.get("role") == "visitor"]
            fields = ("rfid_uid", "student_number", "name", "role", "registered_at")
        else:
            rows = app.logs
            if choice == "Missing time out":
                rows = [r for r in rows if r.get("flag") == "MISSING_TIME_OUT"]
            elif choice == "Overstay":
                rows = [r for r in rows if r.get("flag") == "OVERSTAY"]
            elif choice == "Still in":
                rows = [r for r in rows if not r.get("time_out") and not r.get("flag")]
            fields = ("name", "student_number", "role", "time_in", "time_out", "flag")
        if not query:
            return rows
        return [r for r in rows if query in " ".join(str(r.get(k, "")) for k in fields).casefold()]

    # ---- drawing ---------------------------------------------------------------------
    def render(self):
        app, t = self.app, self.app.t
        for child in self.table.winfo_children():
            child.destroy()

        users_view = self.kind == "users"
        if users_view:
            columns, weights = ["", "UID", "Student No.", "Name", "Role", "Type", "Registered At"], [0, 2, 2, 3, 1, 1, 2]
        else:
            columns, weights = ["Name", "Student No.", "Role", "Time In", "Time Out", "Status"], [3, 2, 1, 2, 2, 2]

        for column, title in enumerate(columns):
            weight = weights[column]
            self.table.grid_columnconfigure(column, weight=weight, minsize=36 if weight == 0 else 70)
            ctk.CTkLabel(self.table, text=title, anchor="w", text_color=t["muted"],
                         font=ctk.CTkFont(size=10, weight="bold"))\
                .grid(row=0, column=column, sticky="ew", padx=9, pady=(8, 11))

        rows = self.rows()
        size = app.page_size()
        total_pages = max(1, (len(rows) + size - 1) // size)
        self.page = min(self.page, total_pages - 1)
        start = self.page * size
        visible = rows[start:start + size]

        if not visible:
            filtered = bool(self.search.get().strip()) or self.filter_var.get() != "All"
            ctk.CTkLabel(self.table, text="No matching records." if filtered else "No records yet.",
                         text_color=t["muted"], font=ctk.CTkFont(size=12))\
                .grid(row=1, column=0, columnspan=len(columns), sticky="ew", pady=45)
        else:
            for index, data in enumerate(visible):
                row = 1 + index * 2
                (self.user_row if users_view else self.log_row)(row, data)

        first = start + 1 if rows else 0
        label = f"Showing {first}-{start + len(visible)} of {len(rows)} records"
        if not users_view:
            label += "  |  Latest 30 attendance records"
        self.result_count.configure(text=label)
        self.page_label.configure(text=f"{self.page + 1} / {total_pages}")
        self.previous_button.configure(state="normal" if self.page > 0 else "disabled")
        self.next_button.configure(state="normal" if self.page + 1 < total_pages else "disabled")
        if self.delete_button is not None:
            self.update_delete_button()

    def separator(self, row, columns):
        ctk.CTkFrame(self.table, height=1, fg_color=self.app.t["sep"], corner_radius=0)\
            .grid(row=row, column=0, columnspan=columns, sticky="ew", padx=8, pady=(0, 1))

    def user_row(self, row, user):
        app, t = self.app, self.app.t
        uid = user["rfid_uid"]
        ctk.CTkRadioButton(self.table, text="", variable=self.selected, value=uid, width=20,
                           fg_color=t["accent"], hover_color=t["accent_hover"],
                           command=self.update_delete_button)\
            .grid(row=row, column=0, sticky="w", padx=(8, 0), pady=8)
        if user.get("is_expired"):
            card_type = "Expired"
        elif user["is_temp"]:
            card_type = "Temporary"
        else:
            card_type = "Permanent"
        values = (uid, user.get("student_number") or "", format_name_for_display(user), user["role"].title(),
                  card_type, user["registered_at"])
        for column, value in enumerate(values, start=1):
            color = t["cyan"] if column == 1 else t["text"]
            if column == 4:
                color = t["blue"] if value.lower() == "student" else t["purple"]
            elif column in (2, 6):
                color = t["muted"]
            ctk.CTkLabel(self.table, text=str(value), anchor="w", text_color=color,
                         font=ctk.CTkFont(size=10)).grid(row=row, column=column, sticky="ew", padx=9, pady=8)
        self.separator(row + 1, 7)

    def log_row(self, row, log):
        t = self.app.t
        flag = log.get("flag")
        flag_text = {"MISSING_TIME_OUT": "Missing time out", "OVERSTAY": "Overstay"}.get(flag, "")
        time_out = log["time_out"] or ("Not timed out" if flag == "MISSING_TIME_OUT" else "Still in")
        values = (format_name_for_display(log), log.get("student_number") or "", log["role"].title(),
                  log["time_in"] or "--", time_out, flag_text or "OK")
        for column, value in enumerate(values):
            color = t["text"]
            if column == 2:
                color = t["blue"] if value.lower() == "student" else t["purple"]
            elif column == 1:
                color = t["muted"]
            elif column == 4:
                color = t["green"] if value == "Still in" else (t["amber"] if value == "Not timed out" else t["text"])
            elif column == 5:
                color = t["amber"] if flag == "MISSING_TIME_OUT" else (t["red"] if flag == "OVERSTAY" else t["muted"])
            ctk.CTkLabel(self.table, text=str(value), anchor="w", text_color=color,
                         font=ctk.CTkFont(size=10)).grid(row=row, column=column, sticky="ew", padx=9, pady=10)
        self.separator(row + 1, 6)

    def update_delete_button(self):
        if self.delete_button is not None:
            self.delete_button.configure(state="normal" if self.selected.get() else "disabled")
        if self.edit_button is not None:
            selected = next((user for user in self.app.users
                             if user["rfid_uid"] == self.selected.get()), None)
            self.edit_button.configure(
                state="normal" if selected and selected.get("role") == "student" else "disabled"
            )

    def edit_selected(self):
        uid = self.selected.get()
        user = next((item for item in self.app.users if item["rfid_uid"] == uid), None)
        if user and user.get("role") == "student":
            self.app.edit_student(user)

    def delete_selected(self):
        uid = self.selected.get()
        if self.app.delete_card(uid):
            self.selected.set("")


# --------------------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------------------
class RfidApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.theme_mode = self.settings["theme"]
        self.apply_theme()

        self.title("RFID Attendance | Admin")
        self.geometry("1360x850")
        self.minsize(1020, 700)
        self.configure(fg_color=self.t["bg"])

        self.users = []
        self.logs = []
        self.today_count = 0
        self.role_var = tk.StringVar(value="student")
        self.active_nav = "Dashboard"
        self.capture_count = None
        self.capture_animation_token = 0
        self.last_seen_tap_count = None
        self.last_status_check = 0
        self.nav_buttons = {}
        self.pages = {}
        self.tables = {}

        self.build_layout()
        self.refresh_all()
        self.poll_capture()

    # ---- theme -----------------------------------------------------------------------
    @property
    def glass(self):
        return self.theme_mode == "light"

    def apply_theme(self):
        self.t = THEMES[self.theme_mode]
        ctk.set_appearance_mode(self.theme_mode)

    def toggle_theme(self):
        self.set_theme("light" if self.theme_mode == "dark" else "dark")

    def set_theme(self, mode):
        mode = mode.lower()
        if mode == self.theme_mode:
            return
        if hasattr(self, "capture_animation_token"):
            self.stop_capture_animation()
        keep = {}
        if hasattr(self, "uid_entry"):
            keep = {
                "uid": self.uid_entry.get(),
                "surname": self.surname_entry.get(),
                "first_name": self.first_name_entry.get(),
                "middle_name": self.middle_name_entry.get(),
                "student_number": self.student_number_entry.get(),
                "role": self.role_var.get(),
                "time_limit": self.time_limit_input.get(),
            }
        self.theme_mode = mode
        self.settings["theme"] = mode
        save_settings(self.settings)
        self.apply_theme()
        self.configure(fg_color=self.t["bg"])
        for child in self.winfo_children():
            child.destroy()
        self.nav_buttons, self.pages, self.tables = {}, {}, {}
        self.build_layout()
        if keep:
            self.uid_entry.insert(0, keep["uid"])
            self.surname_entry.insert(0, keep["surname"])
            self.first_name_entry.insert(0, keep["first_name"])
            self.middle_name_entry.insert(0, keep["middle_name"])
            self.student_number_entry.insert(0, keep["student_number"])
            self.role_var.set(keep["role"])
            self.time_limit_input.insert(0, keep["time_limit"])
        self.refresh_all()

    def page_size(self):
        try:
            return max(1, int(self.settings.get("page_size", 8)))
        except (TypeError, ValueError):
            return 8

    # ---- styled widget factories -------------------------------------------------------
    def descendants(self, widget):
        found = []
        for child in widget.winfo_children():
            found.append(child)
            found.extend(self.descendants(child))
        return found

    def glow_border(self, widget, rest=None, tree=False):
        """Light mode only: soft purple glow ring while the pointer is over the widget."""
        if not self.glass:
            return widget
        t = self.t
        rest = rest or (lambda: (t["ctrl_border"], 1))

        def inside(event):
            try:
                hit = widget.winfo_containing(event.x_root, event.y_root)
            except Exception:
                return False
            while hit is not None:
                if hit is widget:
                    return True
                hit = getattr(hit, "master", None)
            return False

        def enter(_event=None):
            try:
                widget.configure(border_color=t["glow"], border_width=2)
            except Exception:
                pass

        def leave(event=None):
            if tree and event is not None and inside(event):
                return
            if getattr(widget, "_glow_hold", False):
                return
            try:
                color, width = rest()
                widget.configure(border_color=color, border_width=width)
            except Exception:
                pass

        targets = [widget] + (self.descendants(widget) if tree else [])
        for target in targets:
            try:
                target.bind("<Enter>", enter, add="+")
                target.bind("<Leave>", leave, add="+")
            except Exception:
                pass
        return widget

    def card(self, parent, radius=16):
        t = self.t
        frame = ctk.CTkFrame(parent, fg_color=t["panel"], corner_radius=radius,
                             border_width=1, border_color=t["border"])
        return frame

    def finish_card(self, frame):
        """Call after a card is fully built so hover glow can be attached to it."""
        t = self.t
        if self.glass:
            self.glow_border(frame, rest=lambda: (t["border"], 1), tree=True)
        return frame

    def btn(self, parent, text, command=None, kind="default", width=None, height=36, **extra):
        t = self.t
        styles = {
            "default": (t["panel_2"], t["hover"], t["text"], t["ctrl_border"], 1),
            "primary": (t["primary"], t["primary_hover"], t["primary_text"], t["primary_border"], 1),
            "danger": (t["danger_fg"], t["danger_hover"], t["red"], t["danger_border"], 1),
            "ghost": ("transparent", t["hover"], t["text"], t["ctrl_border"], 0),
        }
        fg, hover, color, border, border_width = styles[kind]
        options = dict(text=text, command=command, height=height, corner_radius=10, fg_color=fg,
                       hover_color=hover, border_width=border_width, border_color=border,
                       text_color=color, font=ctk.CTkFont(size=11, weight="bold"))
        if width:
            options["width"] = width
        options.update(extra)
        button = ctk.CTkButton(parent, **options)
        self.glow_border(button, rest=lambda: (border, border_width))
        return button

    def entry(self, parent, placeholder="", width=None, height=38):
        t = self.t
        options = dict(height=height, fg_color=t["entry"], border_color=t["ctrl_border"],
                       text_color=t["text"], placeholder_text=placeholder,
                       placeholder_text_color=t["muted"], corner_radius=10)
        if width:
            options["width"] = width
        field = ctk.CTkEntry(parent, **options)
        self.glow_border(field)
        if self.glass:
            def focus_in(_e=None):
                field._glow_hold = True
                field.configure(border_color=t["glow"], border_width=2)

            def focus_out(_e=None):
                field._glow_hold = False
                field.configure(border_color=t["ctrl_border"], border_width=1)

            field.bind("<FocusIn>", focus_in, add="+")
            field.bind("<FocusOut>", focus_out, add="+")
        return field

    def menu(self, parent, values, variable=None, command=None, width=150):
        t = self.t
        options = list(values)
        selected = variable or tk.StringVar(master=parent, value=options[0] if options else "")
        search = tk.StringVar(master=parent, value=selected.get())

        def select_item(value):
            if value not in options:
                return
            selected.set(value)
            search.set(value)
            widget.configure(values=options)
            if command:
                command(value)

        widget = ctk.CTkComboBox(
            parent, values=options, variable=search, command=select_item,
            width=width, height=34, corner_radius=8, border_width=1,
            fg_color=t["panel_2"], border_color=t["ctrl_border"],
            button_color=t["panel_2"], button_hover_color=t["hover"],
            text_color=t["text"], dropdown_fg_color=t["panel"],
            dropdown_text_color=t["text"], dropdown_hover_color=t["hover"],
            font=ctk.CTkFont(size=11), dropdown_font=ctk.CTkFont(size=11),
            justify="left",
        )

        def filter_items(_event=None):
            query = widget.get().strip().casefold()
            matches = [item for item in options if query in item.casefold()]
            widget.configure(values=matches or ["No items found"])

        def finish_search(_event=None):
            def restore_or_select():
                query = search.get().strip().casefold()
                match = next((item for item in options if item.casefold() == query), None)
                if match:
                    select_item(match)
                else:
                    search.set(selected.get())
                    widget.configure(values=options)

            widget.after(100, restore_or_select)

        widget._entry.bind("<KeyRelease>", filter_items, add="+")
        widget._entry.bind("<FocusOut>", finish_search, add="+")
        if self.glass:
            def enter(_e=None):
                widget.configure(fg_color=t["hover"], button_color=t["glow"])

            def leave(_e=None):
                widget.configure(fg_color=t["panel_2"], button_color=t["panel_2"])

            try:
                widget.bind("<Enter>", enter, add="+")
                widget.bind("<Leave>", leave, add="+")
            except Exception:
                pass
        return widget

    def label(self, parent, text, size=11, bold=False, color=None, **kw):
        return ctk.CTkLabel(parent, text=text, text_color=color or self.t["text"],
                            font=ctk.CTkFont(size=size, weight="bold" if bold else "normal"), **kw)

    def card_title(self, parent, title, subtitle=None):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        self.label(box, title, 15, True).pack(anchor="w")
        if subtitle:
            self.label(box, subtitle, 10, color=self.t["muted"]).pack(anchor="w", pady=(3, 0))
        return box

    # ---- layout ------------------------------------------------------------------------
    def build_layout(self):
        t = self.t
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.build_sidebar()

        content = ctk.CTkFrame(self, fg_color=t["bg"], corner_radius=0)
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)
        self.build_header(content)

        host = ctk.CTkFrame(content, fg_color="transparent")
        host.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 22))
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)

        builders = {
            "Dashboard": self.build_dashboard_page,
            "Register Card": self.build_register_page,
            "Registered Cards": self.build_cards_page,
            "Recent Taps": self.build_taps_page,
            "Reports": self.build_reports_page,
            "Settings": self.build_settings_page,
        }
        for name, builder in builders.items():
            frame = ctk.CTkFrame(host, fg_color="transparent")
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_columnconfigure(0, weight=1)
            builder(frame)
            self.pages[name] = frame
        self.show_page(self.active_nav)

    def show_page(self, name):
        for key, frame in self.pages.items():
            if key == name:
                frame.grid()
            else:
                frame.grid_remove()
        title, subtitle = PAGE_META[name]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=subtitle)

    def build_sidebar(self):
        t = self.t
        sidebar = ctk.CTkFrame(self, width=218, fg_color=t["sidebar"], corner_radius=0,
                               border_width=1, border_color=t["border"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        identity = ctk.CTkFrame(sidebar, fg_color="transparent")
        identity.pack(fill="x", padx=21, pady=(27, 32))
        ctk.CTkLabel(identity, text="R", width=38, height=38, corner_radius=12, fg_color=t["logo_bg"],
                     text_color=t["cyan"], font=ctk.CTkFont(size=19, weight="bold")).pack(side="left")
        ctk.CTkLabel(identity, text="RFID\nATTENDANCE", justify="left", text_color=t["text"],
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=11)

        for label in PAGES:
            button = ctk.CTkButton(
                sidebar, text=label, anchor="w", height=42, corner_radius=10,
                fg_color="transparent", hover_color=t["nav_hover"], text_color=t["nav_text"],
                border_width=0, border_color=t["glow"], font=ctk.CTkFont(size=12),
                command=lambda item=label: self.navigate(item))
            button.pack(fill="x", padx=13, pady=3)
            self.nav_buttons[label] = button
            self.glow_border(button, rest=lambda item=label: self.nav_rest(item))
        self.style_nav()

        ctk.CTkFrame(sidebar, fg_color="transparent").pack(fill="both", expand=True)
        help_panel = self.card(sidebar, radius=11)
        help_panel.pack(fill="x", padx=14, pady=(8, 17))
        self.label(help_panel, "Need help?", 12, True).pack(anchor="w", padx=13, pady=(12, 3))
        self.label(help_panel, "See PROJECT_GUIDE.md for setup.", 10, color=t["muted"],
                   wraplength=165, justify="left").pack(anchor="w", padx=13, pady=(0, 12))
        self.finish_card(help_panel)

    def nav_rest(self, item):
        if item == self.active_nav:
            return (self.t["glow"] if self.glass else self.t["sidebar"], 1 if self.glass else 0)
        return (self.t["sidebar"], 0)

    def style_nav(self):
        t = self.t
        for label, button in self.nav_buttons.items():
            selected = label == self.active_nav
            color, width = self.nav_rest(label)
            button.configure(fg_color=t["nav_active"] if selected else "transparent",
                             text_color=t["text"] if selected else t["nav_text"],
                             border_color=color, border_width=width)

    def build_header(self, parent):
        t = self.t
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(24, 14))
        header.grid_columnconfigure(0, weight=1)

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w")
        self.page_title = self.label(titles, "RFID Attendance", 25, True)
        self.page_title.pack(anchor="w")
        self.page_subtitle = self.label(titles, "", 11, color=t["muted"])
        self.page_subtitle.pack(anchor="w", pady=(3, 0))

        status_group = ctk.CTkFrame(header, fg_color="transparent")
        status_group.grid(row=0, column=1, sticky="e", padx=(12, 12))
        self.server_status_dot = self.label(status_group, "●", 11, color=t["amber"])
        self.server_status_dot.pack(side="left", padx=(0, 5))
        self.server_status_text = self.label(status_group, "Checking server", 10, True, t["muted"])
        self.server_status_text.pack(side="left", padx=(0, 15))
        self.reader_status_dot = self.label(status_group, "●", 11, color=t["amber"])
        self.reader_status_dot.pack(side="left", padx=(0, 5))
        self.reader_status_text = self.label(status_group, "Reader not verified", 10, True, t["amber"])
        self.reader_status_text.pack(side="left")

        profile = self.card(header, radius=9)
        profile.configure(fg_color=t["panel_2"], border_color=t["ctrl_border"])
        profile.grid(row=0, column=2, sticky="e")
        ctk.CTkLabel(profile, text="A", width=28, height=28, corner_radius=14, fg_color=t["avatar"],
                     text_color=t["text"], font=ctk.CTkFont(size=11, weight="bold"))\
            .pack(side="left", padx=(7, 8), pady=6)
        self.label(profile, "Admin", 10, True).pack(side="left", padx=(0, 8))
        toggle = self.btn(profile, "☀" if self.theme_mode == "dark" else "☾",
                          command=lambda: self.after(20, self.toggle_theme),
                          kind="ghost", width=34, height=28)
        toggle.pack(side="left", padx=(0, 8), pady=6)

    # ---- Dashboard page ----------------------------------------------------------------
    def build_dashboard_page(self, page):
        t = self.t
        page.grid_rowconfigure(1, weight=1)

        self.summary_cards = {}
        summary = ctk.CTkFrame(page, fg_color="transparent")
        summary.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        for idx in range(4):
            summary.grid_columnconfigure(idx, weight=1)
        for index, (label, color, footer) in enumerate([
            ("Total Cards", t["blue"], "Registered"), ("Students", t["green"], "Active"),
            ("Visitors", t["amber"], "Temp"), ("Today", t["cyan"], "Visits"),
        ]):
            card = self.card(summary)
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 12) if index < 3 else 0)
            self.label(card, label, 10, True, t["muted"]).pack(anchor="w", padx=16, pady=(14, 0))
            value = self.label(card, "0", 28, True)
            value.pack(anchor="w", padx=16, pady=(6, 0))
            self.label(card, footer, 10, color=color).pack(anchor="w", padx=16, pady=(0, 14))
            self.summary_cards[label] = value
            self.finish_card(card)

        body = ctk.CTkFrame(page, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        activity = self.card(body)
        activity.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        head = ctk.CTkFrame(activity, fg_color="transparent")
        head.pack(fill="x", padx=21, pady=(18, 8))
        self.card_title(head, "Latest activity", "The most recent taps on the reader").pack(side="left")
        self.btn(head, "View all", command=lambda: self.navigate("Recent Taps"),
                 width=84, height=32).pack(side="right")
        self.activity_box = ctk.CTkFrame(activity, fg_color="transparent")
        self.activity_box.pack(fill="both", expand=True, padx=21, pady=(0, 16))
        self.finish_card(activity)

        side = ctk.CTkFrame(body, fg_color="transparent")
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)

        quick = self.card(side)
        quick.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.card_title(quick, "Quick actions").pack(anchor="w", padx=21, pady=(18, 10))
        for text, target, kind in (("Register a new card", "Register Card", "primary"),
                                   ("Browse registered cards", "Registered Cards", "default"),
                                   ("Open attendance report", "Reports", "default")):
            self.btn(quick, text, command=lambda dest=target: self.navigate(dest), kind=kind,
                     height=38).pack(fill="x", padx=21, pady=(0, 8))
        ctk.CTkFrame(quick, height=6, fg_color="transparent").pack()
        self.finish_card(quick)

        flags = self.card(side)
        flags.grid(row=1, column=0, sticky="ew")
        self.card_title(flags, "Attendance flags", "Based on the latest records").pack(anchor="w", padx=21, pady=(18, 8))
        self.flag_labels = {}
        for name, color in (("Still in", t["green"]), ("Missing time out", t["amber"]), ("Overstay", t["red"])):
            row = ctk.CTkFrame(flags, fg_color="transparent")
            row.pack(fill="x", padx=21, pady=4)
            self.label(row, name, 11, color=t["label"]).pack(side="left")
            value = self.label(row, "0", 14, True, color)
            value.pack(side="right")
            self.flag_labels[name] = value
        ctk.CTkFrame(flags, height=10, fg_color="transparent").pack()
        self.finish_card(flags)

    def fill_activity(self):
        if not hasattr(self, "activity_box"):
            return
        t = self.t
        for child in self.activity_box.winfo_children():
            child.destroy()
        recent = self.logs[:7]
        if not recent:
            self.label(self.activity_box, "No taps yet. Tap a card on the reader.", 12,
                       color=t["muted"]).pack(pady=50)
            return
        for col, weight in enumerate((3, 1, 2, 2)):
            self.activity_box.grid_columnconfigure(col, weight=weight)
        for row, log in enumerate(recent):
            flag = log.get("flag")
            status = {"MISSING_TIME_OUT": "Missing time out", "OVERSTAY": "Overstay"}.get(flag)
            if not status:
                status = "Completed" if log.get("time_out") else "Still in"
            color = t["amber"] if flag == "MISSING_TIME_OUT" else (
                t["red"] if flag == "OVERSTAY" else (t["muted"] if log.get("time_out") else t["green"]))
            role = log["role"].title()
            role_color = t["blue"] if role.lower() == "student" else t["purple"]
            for col, (text, tint, bold) in enumerate(((format_name_for_display(log), t["text"], True),
                                                      (role, role_color, False),
                                                      (log["time_in"] or "--", t["muted"], False),
                                                      (status, color, True))):
                self.label(self.activity_box, text, 10, bold, tint, anchor="w")\
                    .grid(row=row * 2, column=col, sticky="ew", padx=6, pady=9)
            ctk.CTkFrame(self.activity_box, height=1, fg_color=t["sep"], corner_radius=0)\
                .grid(row=row * 2 + 1, column=0, columnspan=4, sticky="ew")

    def update_dashboard(self):
        if not hasattr(self, "summary_cards"):
            return
        students = sum(1 for u in self.users if u.get("role") == "student")
        visitors = sum(1 for u in self.users if u.get("role") == "visitor")
        stats = {"Total Cards": len(self.users), "Students": students,
                 "Visitors": visitors, "Today": self.today_count}
        for label, widget in self.summary_cards.items():
            widget.configure(text=str(stats.get(label, 0)))
        counts = self.report_counts()
        self.flag_labels["Still in"].configure(text=str(counts["still_in"]))
        self.flag_labels["Missing time out"].configure(text=str(counts["missing"]))
        self.flag_labels["Overstay"].configure(text=str(counts["overstay"]))
        self.fill_activity()

    # ---- Register Card page ------------------------------------------------------------
    def build_register_page(self, page):
        t = self.t
        page.grid_rowconfigure(1, weight=1)

        panel = self.card(page)
        panel.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        panel.grid_columnconfigure(0, weight=1)
        self.card_title(panel, "Register a New Card",
                        "Press Capture UID, tap the card on the reader, then enter the person's details.")\
            .grid(row=0, column=0, sticky="w", padx=21, pady=(18, 12))
        ctk.CTkFrame(panel, height=1, fg_color=t["sep"], corner_radius=0)\
            .grid(row=1, column=0, sticky="ew", padx=21)

        fields = ctk.CTkFrame(panel, fg_color="transparent")
        fields.grid(row=2, column=0, sticky="ew", padx=21, pady=(14, 17))
        fields.grid_columnconfigure(1, weight=1)
        fields.grid_columnconfigure(3, weight=1)

        self.field_label(fields, "Card UID").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.uid_entry = self.entry(fields, "Scan or enter card UID", height=39)
        self.uid_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.capture_button = self.btn(fields, "Capture UID", command=self.start_capture, height=39)
        self.capture_button.grid(row=0, column=2, padx=(0, 20))

        self.field_label(fields, "Type").grid(row=0, column=3, sticky="w", padx=(4, 9))
        choices = ctk.CTkFrame(fields, fg_color="transparent")
        choices.grid(row=0, column=4, sticky="w")
        for text, value in (("Student", "student"), ("Visitor", "visitor")):
            ctk.CTkRadioButton(choices, text=text, variable=self.role_var, value=value,
                               fg_color=t["accent"], hover_color=t["accent_hover"],
                               text_color=t["text"], font=ctk.CTkFont(size=11))\
                .pack(side="left", padx=(0, 18))

        self.role_var.trace_add("write", lambda *_args: self._on_role_change())

        self.student_name_frame = ctk.CTkFrame(fields, fg_color="transparent")
        self.student_name_frame.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(12, 0))
        self.student_name_frame.grid_columnconfigure(1, weight=1)
        self.student_name_frame.grid_columnconfigure(3, weight=1)
        self.field_label(self.student_name_frame, "Surname").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.surname_entry = self.entry(self.student_name_frame, "Required", height=39)
        self.surname_entry.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        self.field_label(self.student_name_frame, "First Name").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.first_name_entry = self.entry(self.student_name_frame, "Required", height=39)
        self.first_name_entry.grid(row=0, column=3, sticky="ew")
        self.field_label(self.student_name_frame, "Middle Name").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(9, 0))
        self.middle_name_entry = self.entry(self.student_name_frame, "Optional", height=39)
        self.middle_name_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(9, 0))
        number_row = ctk.CTkFrame(fields, fg_color="transparent")
        number_row.grid(row=2, column=0, columnspan=5, sticky="w", pady=(10, 0))
        self.student_number_label = self.field_label(number_row, "Student Number (required)")
        self.student_number_label.pack(side="left", padx=(0, 10))
        self.student_number_entry = self.entry(number_row, "Enter student number", height=36, width=220)
        self.student_number_entry.pack(side="left")

        self.time_limit_frame = ctk.CTkFrame(fields, fg_color="transparent")
        self.time_limit_frame.grid(row=3, column=0, columnspan=5, sticky="w", pady=(10, 0))
        self.field_label(self.time_limit_frame, "Time Limit").pack(side="left", padx=(0, 8))
        self.time_limit_input = self.entry(self.time_limit_frame, "e.g. 30, 60, 120", height=36, width=110)
        self.time_limit_input.pack(side="left", padx=(0, 6))
        self.time_limit_unit = self.menu(self.time_limit_frame, ["minutes", "hours"],
                                         tk.StringVar(value="minutes"),
                                         command=lambda _v: None, width=84)
        self.time_limit_unit.pack(side="left")
        self.time_limit_frame.grid_remove()
        self._on_role_change()

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", padx=21, pady=(0, 16))
        self.capture_status = self.label(actions, "", 10, color=t["muted"])
        self.capture_status.pack(side="left")
        self.btn(actions, "Save Card", command=self.save_user, kind="primary", width=128,
                 height=38).pack(side="right")
        self.btn(actions, "Clear", command=self.clear_form, width=80, height=38).pack(side="right", padx=(0, 10))
        self.finish_card(panel)

        recent = self.card(page)
        recent.grid(row=1, column=0, sticky="nsew")
        self.card_title(recent, "Recently registered", "The newest cards in the system")\
            .pack(anchor="w", padx=21, pady=(18, 8))
        self.recent_reg_box = ctk.CTkFrame(recent, fg_color="transparent")
        self.recent_reg_box.pack(fill="both", expand=True, padx=21, pady=(0, 16))
        self.finish_card(recent)

    def field_label(self, parent, text):
        return self.label(parent, text, 10, True, self.t["label"])

    def clear_form(self):
        if self.capture_count is not None:
            data = self.api("POST", "/capture/cancel")
            if data is None or not data.get("success"):
                messagebox.showwarning(
                    "Capture still active",
                    "The server could not confirm that card capture was cancelled.",
                    parent=self,
                )
                return
            self.capture_count = None
        self.stop_capture_animation()
        self.uid_entry.delete(0, "end")
        self.first_name_entry.delete(0, "end")
        self.surname_entry.delete(0, "end")
        self.middle_name_entry.delete(0, "end")
        self.student_number_entry.delete(0, "end")
        self.role_var.set("student")
        self.capture_status.configure(text="", text_color=self.t["muted"])
        if hasattr(self, "time_limit_input"):
            self.time_limit_input.delete(0, "end")
        if hasattr(self, "time_limit_frame"):
            self.time_limit_frame.grid_remove()
            self.time_limit_unit.configure(values=["minutes", "hours"])
            try:
                self.time_limit_unit.set("minutes")
            except Exception:
                pass

    def fill_recent_registered(self):
        if not hasattr(self, "recent_reg_box"):
            return
        t = self.t
        for child in self.recent_reg_box.winfo_children():
            child.destroy()
        newest = sorted(self.users, key=lambda u: u.get("registered_at", ""), reverse=True)[:6]
        if not newest:
            self.label(self.recent_reg_box, "No cards registered yet.", 12, color=t["muted"]).pack(pady=40)
            return
        for col, weight in enumerate((2, 2, 3, 1, 2)):
            self.recent_reg_box.grid_columnconfigure(col, weight=weight)
        for row, user in enumerate(newest):
            role = user["role"].title()
            role_color = t["blue"] if role.lower() == "student" else t["purple"]
            for col, (text, tint) in enumerate(((user["rfid_uid"], t["cyan"]),
                                                (user.get("student_number") or "", t["muted"]),
                                                (format_name_for_display(user), t["text"]),
                                                (role, role_color), (user["registered_at"], t["muted"]))):
                self.label(self.recent_reg_box, text, 10, False, tint, anchor="w")\
                    .grid(row=row * 2, column=col, sticky="ew", padx=6, pady=9)
            ctk.CTkFrame(self.recent_reg_box, height=1, fg_color=t["sep"], corner_radius=0)\
                .grid(row=row * 2 + 1, column=0, columnspan=4, sticky="ew")

    # ---- Registered Cards / Recent Taps pages ------------------------------------------
    def build_cards_page(self, page):
        page.grid_rowconfigure(0, weight=1)
        panel = self.card(page)
        panel.grid(row=0, column=0, sticky="nsew")
        self.tables["users"] = DataTable(self, panel, "users")
        self.finish_card(panel)

    def build_taps_page(self, page):
        page.grid_rowconfigure(0, weight=1)
        panel = self.card(page)
        panel.grid(row=0, column=0, sticky="nsew")
        self.tables["logs"] = DataTable(self, panel, "logs")
        self.finish_card(panel)

    # ---- Reports page ------------------------------------------------------------------
    def build_reports_page(self, page):
        t = self.t
        page.grid_columnconfigure(0, weight=3)
        page.grid_columnconfigure(1, weight=2)
        page.grid_rowconfigure(0, weight=1)

        stats = self.card(page)
        stats.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.card_title(stats, "Attendance breakdown", "Attendance totals and currently registered cards")\
            .pack(anchor="w", padx=21, pady=(18, 10))
        self.report_total = self.label(stats, "0 records", 24, True)
        self.report_total.pack(anchor="w", padx=21, pady=(0, 10))
        self.report_rows = {}
        for name, color in (("Completed visits", t["green"]), ("Still in", t["cyan"]),
                            ("Missing time out", t["amber"]), ("Overstay", t["red"]),
                            ("Registered students", t["blue"]), ("Registered visitors", t["purple"])):
            row = ctk.CTkFrame(stats, fg_color="transparent")
            row.pack(fill="x", padx=21, pady=7)
            top = ctk.CTkFrame(row, fg_color="transparent")
            top.pack(fill="x")
            self.label(top, name, 11, color=t["label"]).pack(side="left")
            count = self.label(top, "0", 11, True)
            count.pack(side="right")
            bar = ctk.CTkProgressBar(row, height=8, corner_radius=6, fg_color=t["panel_2"],
                                     progress_color=color)
            bar.pack(fill="x", pady=(5, 0))
            bar.set(0)
            self.report_rows[name] = (bar, count)
        self.finish_card(stats)

        side = ctk.CTkFrame(page, fg_color="transparent")
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        export = self.card(side)
        export.grid(row=0, column=0, sticky="ew")
        self.card_title(export, "Export data", "Save the current data as a CSV file")\
            .pack(anchor="w", padx=21, pady=(18, 10))
        self.btn(export, "Export registered cards", command=lambda: self.export_csv("users"),
                 kind="primary", height=38).pack(fill="x", padx=21, pady=(0, 8))
        self.btn(export, "Export attendance records", command=lambda: self.export_csv("logs"),
                 height=38).pack(fill="x", padx=21, pady=(0, 8))
        self.btn(export, "Recalculate report", command=self.refresh_all, kind="ghost",
                 height=38).pack(fill="x", padx=21, pady=(0, 18))
        self.finish_card(export)

    def report_counts(self):
        logs = self.logs
        flags = [log.get("flag") for log in logs]
        return {
            "total": len(logs),
            "completed": sum(1 for log in logs if log.get("time_out")),
            "still_in": sum(1 for log in logs if not log.get("time_out") and not log.get("flag")),
            "missing": flags.count("MISSING_TIME_OUT"),
            "overstay": flags.count("OVERSTAY"),
            "students": sum(1 for user in self.users if user.get("role") == "student"),
            "visitors": sum(1 for user in self.users if user.get("role") == "visitor"),
            "registered_total": len(self.users),
        }

    def update_report(self):
        if not hasattr(self, "report_rows"):
            return
        counts = self.report_counts()
        total = counts["total"]
        self.report_total.configure(text=f"{total} record{'s' if total != 1 else ''}")
        mapping = {"Completed visits": "completed", "Still in": "still_in", "Missing time out": "missing",
                   "Overstay": "overstay", "Registered students": "students",
                   "Registered visitors": "visitors"}
        for name, key in mapping.items():
            bar, label = self.report_rows[name]
            label.configure(text=str(counts[key]))
            denominator = counts["registered_total"] if key in ("students", "visitors") else total
            bar.set(counts[key] / denominator if denominator else 0)

    def export_csv(self, kind):
        if kind == "users":
            rows, fields, default = self.users, [
                "rfid_uid", "student_number", "surname", "first_name", "middle_name",
                "name", "role", "is_temp", "registered_at"
            ], "registered_cards.csv"
        else:
            rows, fields, default = self.logs, [
                "student_number", "name", "role", "time_in", "time_out", "flag"
            ], "attendance_records.csv"
        if not rows:
            messagebox.showinfo("Nothing to export", "There are no records to export yet.", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", initialfile=default,
                                            filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        except OSError as error:
            messagebox.showerror("Export failed", str(error), parent=self)
            return
        messagebox.showinfo("Export complete", f"Saved {len(rows)} rows to:\n{path}", parent=self)

    # ---- Settings page -----------------------------------------------------------------
    def build_settings_page(self, page):
        t = self.t
        page.grid_columnconfigure(0, weight=1)

        server = self.card(page)
        server.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.card_title(server, "Server connection", "Address of the Flask server (app.py)")\
            .pack(anchor="w", padx=21, pady=(18, 10))
        row = ctk.CTkFrame(server, fg_color="transparent")
        row.pack(fill="x", padx=21, pady=(0, 6))
        row.grid_columnconfigure(0, weight=1)
        self.server_entry = self.entry(row, "http://127.0.0.1:5001", height=38)
        self.server_entry.insert(0, self.settings["server"])
        self.server_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.btn(row, "Save & test", command=self.apply_server, kind="primary", width=120,
                 height=38).grid(row=0, column=1)
        self.settings_status = self.label(server, "", 10, color=t["muted"])
        self.settings_status.pack(anchor="w", padx=21, pady=(0, 16))
        self.finish_card(server)

        prefs = self.card(page)
        prefs.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self.card_title(prefs, "Preferences", "Saved automatically to ui_settings.json")\
            .pack(anchor="w", padx=21, pady=(18, 10))
        grid = ctk.CTkFrame(prefs, fg_color="transparent")
        grid.pack(fill="x", padx=21, pady=(0, 18))
        grid.grid_columnconfigure(1, weight=1)
        self.label(grid, "Rows per page", 11, color=t["label"]).grid(row=0, column=0, sticky="w", pady=6)
        self.page_size_var = tk.StringVar(value=str(self.page_size()))
        self.menu(grid, ["5", "8", "10", "15"], self.page_size_var, self.set_page_size, width=100)\
            .grid(row=0, column=1, sticky="e", pady=6)
        self.label(grid, "Appearance", 11, color=t["label"]).grid(row=1, column=0, sticky="w", pady=6)
        segmented = ctk.CTkSegmentedButton(grid, values=["Dark", "Light"],
                                           command=lambda v: self.after(20, lambda: self.set_theme(v)),
                                           selected_color=t["accent"], selected_hover_color=t["accent_hover"],
                                           unselected_color=t["panel_2"], unselected_hover_color=t["hover"],
                                           text_color=t["text"], fg_color=t["panel_2"])
        segmented.set(self.theme_mode.title())
        segmented.grid(row=1, column=1, sticky="e", pady=6)
        self.finish_card(prefs)

        about = self.card(page)
        about.grid(row=2, column=0, sticky="ew")
        self.card_title(about, "About", "RFID Attendance admin panel")\
            .pack(anchor="w", padx=21, pady=(18, 6))
        self.label(about, "Cards are registered here and the reader (app.py) records time in / time out. "
                          "Records older than the latest 30 are not shown.", 10, color=t["muted"],
                   wraplength=760, justify="left").pack(anchor="w", padx=21, pady=(0, 18))
        self.finish_card(about)

    def apply_server(self):
        url = self.server_entry.get().strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("Invalid address", "The address must start with http:// or https://", parent=self)
            return
        self.settings["server"] = url
        save_settings(self.settings)
        data = self.api("GET", "/last-tap")
        if data is None:
            self.settings_status.configure(text="Saved, but the server did not respond.", text_color=self.t["red"])
        else:
            self.settings_status.configure(text="Saved. Connected to the server.", text_color=self.t["green"])
            self.refresh_all()

    def set_page_size(self, value):
        self.settings["page_size"] = int(value)
        save_settings(self.settings)
        for table in self.tables.values():
            table.page = 0
        self.render_everything()

    # ---- navigation ----------------------------------------------------------------------
    def navigate(self, item):
        self.active_nav = item
        self.style_nav()
        self.show_page(item)
        if item == "Register Card":
            self.uid_entry.focus_set()
        elif item == "Registered Cards":
            self.refresh_users()
        elif item in ("Recent Taps", "Reports"):
            self.refresh_logs()
        elif item == "Dashboard":
            self.refresh_all()

    # ---- Visitor time-limit toggle -------------------------------------------------------
    def _on_role_change(self):
        if not hasattr(self, "time_limit_frame"):
            return
        if self.role_var.get() == "visitor":
            self.time_limit_frame.grid()
            self.student_number_label.configure(text="Student Number (optional)")
        else:
            self.time_limit_frame.grid_remove()
            self.student_number_label.configure(text="Student Number (required)")

    # ---- server ----------------------------------------------------------------------------
    def api(self, method, path, body=None):
        try:
            server = self.settings["server"].rstrip("/")
            response = requests.request(method, server + path, json=body, timeout=3)
            response.raise_for_status()
            self.set_server_status(True)
            return response.json()
        except (requests.exceptions.RequestException, ValueError):
            self.set_server_status(False)
            return None

    def set_server_status(self, online):
        if not hasattr(self, "server_status_dot"):
            return
        t = self.t
        color = t["green"] if online else t["red"]
        self.server_status_dot.configure(text_color=color)
        self.server_status_text.configure(text="Server online" if online else "Server offline", text_color=color)
        if not online:
            self.set_reader_status("Server unavailable", t["red"])
        elif self.reader_status_text.cget("text") == "Server unavailable":
            self.set_reader_status("Reader not verified", t["amber"])

    def set_reader_status(self, text, color=None):
        color = color or self.t["amber"]
        self.reader_status_dot.configure(text_color=color)
        self.reader_status_text.configure(text=text, text_color=color)

    def start_capture(self):
        self.stop_capture_animation()
        data = self.api("POST", "/capture/start")
        if data is None:
            self.capture_status.configure(text="Start app.py, then try again.", text_color=self.t["red"])
            return
        if not data.get("success"):
            self.capture_status.configure(text=data.get("message", "Capture is already active."),
                                          text_color=self.t["red"])
            return
        self.capture_count = data["count"]
        self.capture_button.configure(text="Waiting for card...", state="disabled")
        self.capture_status.configure(text="PLEASE TAP YOUR RFID CARD", text_color=self.t["amber"])
        self.set_reader_status("Waiting for card", self.t["amber"])

    def stop_capture_animation(self):
        self.capture_animation_token += 1
        if hasattr(self, "capture_button"):
            self.capture_button.configure(
                text="Capture UID",
                fg_color=self.t["panel_2"],
                hover_color=self.t["hover"],
                text_color=self.t["text"],
                state="normal",
            )

    def animate_capture_success(self, token, step=0):
        if token != self.capture_animation_token or not self.capture_button.winfo_exists():
            return
        if step >= 6:
            self.stop_capture_animation()
            return

        bright = step % 2 == 0
        self.capture_button.configure(
            text="Card Read!" if bright else "Capture UID",
            fg_color=self.t["green"] if bright else self.t["panel_2"],
            hover_color=self.t["green"] if bright else self.t["hover"],
            text_color=self.t["bg"] if bright else self.t["text"],
        )
        self.after(140, lambda: self.animate_capture_success(token, step + 1))

    def poll_capture(self):
        now = time.monotonic()
        if self.capture_count is not None or now - self.last_status_check >= 5:
            self.last_status_check = now
            try:
                server = self.settings["server"].rstrip("/")
                response = requests.get(server + "/last-tap", timeout=1.5)
                response.raise_for_status()
                data = response.json()
                self.set_server_status(True)
                if self.last_seen_tap_count is None:
                    self.last_seen_tap_count = data["count"]
                elif data["count"] != self.last_seen_tap_count:
                    self.last_seen_tap_count = data["count"]
                    self.refresh_logs()
                if self.capture_count is not None and data["count"] != self.capture_count:
                    capture_status = data.get("capture_status")
                    if capture_status == "capture_success":
                        self.uid_entry.delete(0, "end")
                        self.uid_entry.insert(0, data["uid"])
                        self.capture_status.configure(
                            text="CARD READ SUCCESSFULLY", text_color=self.t["green"]
                        )
                        self.set_reader_status("Card captured", self.t["green"])
                        self.capture_animation_token += 1
                        self.animate_capture_success(self.capture_animation_token)
                    elif capture_status == "card_already_registered":
                        self.capture_status.configure(
                            text="CARD ALREADY REGISTERED", text_color=self.t["red"]
                        )
                        self.set_reader_status("Card already registered", self.t["red"])
                    else:
                        self.capture_status.configure(
                            text=data.get("capture_message") or "Card read failed.",
                            text_color=self.t["red"],
                        )
                    self.capture_count = None
                    self.capture_button.configure(state="normal", text="Capture UID")
            except (requests.exceptions.RequestException, ValueError, KeyError):
                self.set_server_status(False)
                if self.capture_count is not None:
                    self.capture_status.configure(text="Server unavailable. Check app.py.", text_color=self.t["red"])
                    try:
                        requests.post(
                            self.settings["server"].rstrip("/") + "/capture/cancel",
                            timeout=1.5,
                        )
                    except requests.exceptions.RequestException:
                        self.capture_status.configure(
                            text="Server unavailable. Capture cancellation was not confirmed.",
                            text_color=self.t["red"],
                        )
                    self.capture_count = None
                    self.capture_button.configure(state="normal", text="Capture UID")
        self.after(700, self.poll_capture)

    # ---- data actions ----------------------------------------------------------------------
    def save_user(self):
        uid = "".join(self.uid_entry.get().split()).upper()
        role = self.role_var.get()
        surname = self.surname_entry.get().strip()
        first_name = self.first_name_entry.get().strip()
        middle_name = self.middle_name_entry.get().strip()
        student_number = self.student_number_entry.get().strip()
        name = " ".join(part for part in (surname, first_name, middle_name) if part)
        if not uid or not name:
            messagebox.showwarning("Missing information", "Capture or enter a card UID and complete the required name fields.", parent=self)
            return
        if not surname or not first_name:
            messagebox.showwarning("Missing information", "Surname and first name are required.", parent=self)
            return
        if role == "student" and not student_number:
            messagebox.showwarning("Missing information", "Student number is required for students.", parent=self)
            return
        body = {
            "uid": uid, "role": role, "name": name, "surname": surname,
            "first_name": first_name, "middle_name": middle_name,
            "student_number": student_number or None,
        }
        # Add time limit for visitor cards
        if role == "visitor":
            limit_text = self.time_limit_input.get().strip() if hasattr(self, "time_limit_input") else ""
            if limit_text:
                try:
                    val = int(limit_text)
                    unit = self.time_limit_unit.get() if hasattr(self, "time_limit_unit") else "minutes"
                    if val < 1:
                        raise ValueError
                    body["time_limit_minutes"] = val if unit == "minutes" else val * 60
                except ValueError:
                    messagebox.showwarning("Invalid time limit", "Please enter a valid number for the time limit.", parent=self)
                    return
        data = self.api("POST", "/users", body)
        if data is None:
            messagebox.showerror("Server unavailable", "Could not save the card. Check that app.py is running.", parent=self)
        elif data.get("success"):
            messagebox.showinfo("Card saved", data["message"], parent=self)
            self.clear_form()
            self.refresh_users()
        else:
            messagebox.showerror("Card not saved", data.get("message", "Please check the card details."), parent=self)

    def edit_student(self, user):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Edit student name")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("440x340")
        dialog.grid_columnconfigure(1, weight=1)
        self.label(dialog, "Correct student information", 16, True)\
            .grid(row=0, column=0, columnspan=2, sticky="w", padx=22, pady=(20, 6))
        self.label(dialog, f"Student number: {user.get('student_number') or 'not assigned'}", 10,
                   color=self.t["muted"]).grid(row=1, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 14))

        words = user.get("name", "").split()
        legacy_first = words[0] if words else ""
        legacy_surname = words[-1] if len(words) > 1 else ""
        legacy_middle = " ".join(words[1:-1]) if len(words) > 2 else ""
        surname = user.get("surname") or legacy_surname
        first_name = user.get("first_name") or legacy_first
        middle_name = user.get("middle_name") or legacy_middle
        entries = {}
        for row, (key, label, value) in enumerate((
                ("surname", "Surname", surname),
                ("first_name", "First name", first_name),
                ("middle_name", "Middle name", middle_name)), start=2):
            self.field_label(dialog, label).grid(row=row, column=0, sticky="w", padx=(22, 12), pady=7)
            entry = self.entry(dialog, label, height=36)
            entry.insert(0, value)
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 22), pady=7)
            entries[key] = entry

        def save():
            fields = {key: entry.get().strip() for key, entry in entries.items()}
            if not fields["surname"] or not fields["first_name"]:
                messagebox.showwarning("Missing information", "Surname and first name are required.", parent=dialog)
                return
            result = self.api("PATCH", f"/users/{user['id']}", fields)
            if result is None:
                messagebox.showerror("Update failed", "The server did not respond.", parent=dialog)
            elif not result.get("success"):
                messagebox.showerror("Update failed", result.get("message", "Student name was not updated."), parent=dialog)
            else:
                dialog.destroy()
                self.refresh_all()
                messagebox.showinfo("Student updated", result["message"], parent=self)

        self.btn(dialog, "Cancel", command=dialog.destroy, width=90, height=36)\
            .grid(row=5, column=0, padx=(22, 8), pady=(16, 20), sticky="e")
        self.btn(dialog, "Save changes", command=save, kind="primary", width=132, height=36)\
            .grid(row=5, column=1, padx=(0, 22), pady=(16, 20), sticky="e")

    def delete_card(self, uid):
        if not uid:
            messagebox.showwarning("No card selected", "Select a registered card before deleting.", parent=self)
            return False
        if not messagebox.askyesno("Delete card", f"Delete card {uid} and its attendance history?", parent=self):
            return False
        result = self.api("DELETE", f"/users/{uid}")
        if result is None:
            messagebox.showerror("Server unavailable", "The card could not be deleted.", parent=self)
            return False
        self.refresh_all()
        return True

    def refresh_users(self):
        data = self.api("GET", "/users")
        if data is not None:
            self.users = data
            self.render_everything()

    def refresh_logs(self):
        data = self.api("GET", "/logs")
        if data is not None:
            self.logs = data
            count = self.api("GET", "/today-count")
            if count is not None:
                self.today_count = count.get("count", 0)
            self.render_everything()

    def refresh_all(self):
        users = self.api("GET", "/users")
        if users is not None:
            self.users = users
        logs = self.api("GET", "/logs")
        if logs is not None:
            self.logs = logs
            count = self.api("GET", "/today-count")
            if count is not None:
                self.today_count = count.get("count", 0)
        self.render_everything()

    def render_everything(self):
        self.update_dashboard()
        self.update_report()
        self.fill_recent_registered()
        for table in self.tables.values():
            table.render()


if __name__ == "__main__":
    app = RfidApp()
    app.state("zoomed")
    app.mainloop()