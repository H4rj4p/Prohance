"""Prohance Flask chat application factory."""

from flask import Flask

from app.config import load_settings
from app.routes import bp


def create_app() -> Flask:
    settings = load_settings()
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config["SETTINGS"] = settings
    app.register_blueprint(bp)
    return app
