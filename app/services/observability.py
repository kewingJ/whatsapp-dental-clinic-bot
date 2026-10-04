"""Optional LangSmith tracing helpers for the clinic assistant."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_langsmith_tracing_enabled() -> bool:
    """Return whether LangSmith tracing is enabled via environment variables."""
    return _is_truthy(os.getenv("LANGSMITH_TRACING", "false"))


def get_langsmith_project() -> str:
    """Return the active LangSmith project name."""
    return os.getenv("LANGSMITH_PROJECT", "default").strip() or "default"


def get_langsmith_endpoint() -> str:
    """Return the configured LangSmith endpoint."""
    return os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").strip()


def langsmith_sdk_available() -> bool:
    """Return whether the LangSmith SDK is importable."""
    try:
        import langsmith  # noqa: F401
    except ImportError:
        return False
    return True


def traceable_function(*decorator_args, **decorator_kwargs):
    """Return a LangSmith traceable decorator, or a no-op fallback."""

    def _identity(func: Callable[..., Any]) -> Callable[..., Any]:
        return func

    if not is_langsmith_tracing_enabled():
        return _identity

    try:
        from langsmith import traceable
    except ImportError:
        logging.warning(
            "LangSmith tracing esta activado pero la libreria 'langsmith' no esta instalada. "
            "El tracing quedara deshabilitado."
        )
        return _identity

    return traceable(*decorator_args, **decorator_kwargs)


def get_openai_client(api_key: str, base_url: str | None = None) -> OpenAI:
    """Build an OpenAI client, optionally wrapped for LangSmith tracing."""
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    if not is_langsmith_tracing_enabled():
        return client

    try:
        from langsmith.wrappers import wrap_openai
    except ImportError:
        logging.warning(
            "LangSmith tracing esta activado pero la libreria 'langsmith' no esta instalada. "
            "El cliente OpenAI seguira funcionando sin tracing."
        )
        return client

    return wrap_openai(client)


def log_langsmith_configuration() -> None:
    """Log the current LangSmith tracing configuration at startup."""
    tracing_enabled = is_langsmith_tracing_enabled()
    logging.info(
        "LangSmith tracing: enabled=%s sdk_available=%s project=%s endpoint=%s",
        tracing_enabled,
        langsmith_sdk_available(),
        get_langsmith_project(),
        get_langsmith_endpoint(),
    )

