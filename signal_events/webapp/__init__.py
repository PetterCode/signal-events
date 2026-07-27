from __future__ import annotations

from flask import Flask

from .. import config, db, naming


def create_app() -> Flask:
    config.ensure_dirs()
    db.init_db()

    app = Flask(__name__)
    with db.get_connection() as conn:
        app.config["SECRET_KEY"] = db.get_or_create_secret_key(conn)
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB, covers a few photos per report
    # Lets templates show "Händelse <TNR>" instead of an arbitrary
    # database id -- e.g. {{ event_tnr(event.event_time, event.created_at) }}.
    app.jinja_env.globals["event_tnr"] = naming.event_tnr

    from . import routes

    app.register_blueprint(routes.bp)
    return app
