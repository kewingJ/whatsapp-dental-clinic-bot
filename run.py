import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app import create_app
from app.services.observability import log_langsmith_configuration

load_dotenv()
app = create_app()


def _log_runtime_environment() -> None:
    executable = Path(sys.executable).expanduser().resolve()
    valid_venv_paths = {Path(".venv/bin/python").resolve(), Path("venv/bin/python").resolve()}
    in_project_venv = executable in valid_venv_paths
    logging.info("Python executable: %s", executable)
    logging.info("Running inside project venv: %s", in_project_venv)

    google_status = {"service_account": False, "calendar_api": False}
    try:
        from google.oauth2 import service_account  # noqa: F401

        google_status["service_account"] = True
    except ImportError:
        pass

    try:
        from googleapiclient.discovery import build  # noqa: F401

        google_status["calendar_api"] = True
    except ImportError:
        pass

    logging.info(
        "Google dependency status: service_account=%s calendar_api=%s",
        google_status["service_account"],
        google_status["calendar_api"],
    )

    configured_calendar_id = bool(os.getenv("GOOGLE_CALENDAR_ID", "").strip())
    configured_service_account = bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip())
    logging.info(
        "Google Calendar config present: calendar_id=%s service_account_file=%s",
        configured_calendar_id,
        configured_service_account,
    )

    if configured_calendar_id and configured_service_account and not all(google_status.values()):
        logging.warning(
            "Google Calendar esta configurado pero faltan dependencias en este interprete. "
            "Usa .venv/bin/python run.py para arrancar el bot."
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    _log_runtime_environment()
    log_langsmith_configuration()
    logging.info("Starting Flask app on port %s", port)
    app.run(host="0.0.0.0", port=port)
