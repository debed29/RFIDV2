"""Mobile/student API (/api/v1). The ESP32 /tap endpoint is NOT part of this package."""
import database as db

from . import admin, auth, inbox, push, student  # noqa: F401  (importing registers the routes)
from .common import bp
from .notifier import notify_after_tap
from .schema import init_api_schema, purge_user_data


def init_api(app):
    db.init_db()
    init_api_schema()
    app.register_blueprint(bp)
    push.ensure_started()


__all__ = ["init_api", "notify_after_tap", "purge_user_data"]
