"""Notification inbox routes. Every query is scoped to the authenticated student."""
from flask import g, jsonify, request

from .common import ApiError, bp, connect, json_body, now_text, require_auth, to_iso

CATEGORIES = ("attendance", "emergency", "system")


def _item(r):
    return {"id": r["id"], "category": r["category"], "title": r["title"], "body": r["body"],
            "created_at": to_iso(r["created_at"]), "read": r["read_at"] is not None,
            "read_at": to_iso(r["read_at"])}


def _int_arg(name, default, lo, hi):
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ApiError(400, "invalid_argument", f"'{name}' must be a number.")
    if not lo <= v <= hi:
        raise ApiError(400, "invalid_argument", f"'{name}' must be {lo}-{hi}.")
    return v


def _category_arg():
    cat = request.args.get("category")
    if cat is not None and cat not in CATEGORIES:
        raise ApiError(400, "invalid_argument", "Unknown category.")
    return cat


@bp.get("/notifications")
@require_auth
def list_notifications():
    cat, limit = _category_arg(), _int_arg("limit", 30, 1, 50)
    before = _int_arg("before", None, 1, 2**62)
    sql, args = "SELECT * FROM notifications WHERE user_id = ?", [g.user_id]
    if cat:
        sql, args = sql + " AND category = ?", args + [cat]
    if request.args.get("unread") == "1":
        sql += " AND read_at IS NULL"
    if before:
        sql, args = sql + " AND id < ?", args + [before]
    with connect() as c:
        rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit + 1]).fetchall()
    page = rows[:limit]
    return jsonify(items=[_item(r) for r in page], next_before=page[-1]["id"] if len(rows) > limit else None)


@bp.get("/notifications/unread-count")
@require_auth
def unread():
    with connect() as c:
        rows = c.execute("""SELECT category, COUNT(*) AS n FROM notifications
                            WHERE user_id = ? AND read_at IS NULL GROUP BY category""", (g.user_id,)).fetchall()
    by_cat = {c: 0 for c in CATEGORIES}
    by_cat.update({r["category"]: r["n"] for r in rows})
    return jsonify(total=sum(by_cat.values()), by_category=by_cat)


@bp.get("/notifications/<int:nid>")
@require_auth
def notification_detail(nid):
    with connect() as c:
        r = c.execute("SELECT * FROM notifications WHERE id = ? AND user_id = ?", (nid, g.user_id)).fetchone()
    if r is None:
        raise ApiError(404, "not_found", "Notification not found.")
    return jsonify(_item(r))


@bp.post("/notifications/<int:nid>/read")
@require_auth
def mark_read(nid):
    with connect(write=True) as c:
        c.execute("UPDATE notifications SET read_at = ? WHERE id = ? AND user_id = ? AND read_at IS NULL",
                  (now_text(), nid, g.user_id))
        r = c.execute("SELECT * FROM notifications WHERE id = ? AND user_id = ?", (nid, g.user_id)).fetchone()
    if r is None:
        raise ApiError(404, "not_found", "Notification not found.")
    return jsonify(_item(r))


@bp.post("/notifications/read-all")
@require_auth
def mark_all_read():
    cat = None
    if request.get_data(cache=True):
        cat = json_body().get("category")
        if cat is not None and cat not in CATEGORIES:
            raise ApiError(400, "invalid_argument", "Unknown category.")
    sql, args = "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL", [now_text(), g.user_id]
    if cat:
        sql, args = sql + " AND category = ?", args + [cat]
    with connect(write=True) as c:
        n = c.execute(sql, args).rowcount
    return jsonify(updated=n)
