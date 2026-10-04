from flask import Flask

from app.config import load_configurations, configure_logging


def create_app():
    """Application factory used by Flask and local development."""
    app = Flask(__name__)

    load_configurations(app)
    configure_logging()

    from .views import webhook_blueprint

    app.register_blueprint(webhook_blueprint)

    return app
