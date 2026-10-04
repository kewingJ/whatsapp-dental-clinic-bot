import logging
import os
import sys

from dotenv import load_dotenv


def load_configurations(app):
    """Load runtime configuration from environment variables."""
    load_dotenv()
    app.config.update(
        ACCESS_TOKEN=os.getenv("ACCESS_TOKEN"),
        APP_ID=os.getenv("APP_ID"),
        APP_SECRET=os.getenv("APP_SECRET"),
        VERSION=os.getenv("VERSION", "v22.0"),
        PHONE_NUMBER_ID=os.getenv("PHONE_NUMBER_ID"),
        VERIFY_TOKEN=os.getenv("VERIFY_TOKEN"),
        OPENAI_API_KEY=os.getenv("OPENAI_API_KEY"),
        OPENAI_MODEL=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        PUBLIC_URL=os.getenv("PUBLIC_URL"),
        LANGSMITH_TRACING=os.getenv("LANGSMITH_TRACING", "false"),
        LANGSMITH_ENDPOINT=os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
        LANGSMITH_API_KEY=os.getenv("LANGSMITH_API_KEY"),
        LANGSMITH_PROJECT=os.getenv("LANGSMITH_PROJECT", "default"),
        CLINIC_TIMEZONE=os.getenv("CLINIC_TIMEZONE", "America/Managua"),
        GOOGLE_SERVICE_ACCOUNT_FILE=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"),
        GOOGLE_CALENDAR_ID=os.getenv("GOOGLE_CALENDAR_ID"),
    )


def configure_logging():
    """Configure the project-wide logging format."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
