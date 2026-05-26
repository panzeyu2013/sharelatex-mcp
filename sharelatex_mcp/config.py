from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppConfig:
    base_url: str
    email: str
    password: str
    timeout_seconds: int
    allow_insecure_http: bool
    log_level: str


def _get_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() == "true"


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} is not a valid integer") from exc


def load_config() -> AppConfig:
    load_dotenv()

    base_url = _get_required("OVERLEAF_BASE_URL").rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise RuntimeError("OVERLEAF_BASE_URL must start with http:// or https://")

    if base_url.startswith("http://") and not _get_bool("OVERLEAF_ALLOW_INSECURE_HTTP", False):
        raise RuntimeError(
            "You are using an http:// URL. Set OVERLEAF_ALLOW_INSECURE_HTTP=true to proceed."
        )

    timeout_raw = os.getenv("OVERLEAF_TIMEOUT_SECONDS")
    if timeout_raw is not None:
        timeout_seconds = _get_int("OVERLEAF_TIMEOUT_SECONDS", 15)
    else:
        legacy_ms = os.getenv("OVERLEAF_TIMEOUT_MS")
        if legacy_ms is not None:
            logger.warning(
                "OVERLEAF_TIMEOUT_MS is deprecated; use OVERLEAF_TIMEOUT_SECONDS instead. "
                "The old variable name was misleading — its value was already treated as seconds."
            )
            timeout_seconds = _get_int("OVERLEAF_TIMEOUT_MS", 15)
        else:
            timeout_seconds = 15

    return AppConfig(
        base_url=base_url,
        email=_get_required("OVERLEAF_EMAIL"),
        password=_get_required("OVERLEAF_PASSWORD"),
        timeout_seconds=timeout_seconds,
        allow_insecure_http=_get_bool("OVERLEAF_ALLOW_INSECURE_HTTP", False),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
