from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from sharelatex_mcp.validation import validate_project_id

CONFIG_DIR = Path.home() / ".config" / "sharelatex-mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

TEMPLATE = """\
{
  // Base URL of your self-hosted ShareLaTeX / Overleaf instance
  "base_url": "http://your-overleaf-host:2233",
  // Login email
  "email": "your-email@example.com",
  // Login password
  "password": "your-password",
  // HTTP request timeout in seconds (default: 15)
  "timeout_seconds": 15,
  // Set to true if you are using http:// instead of https://
  "allow_insecure_http": false,
  // Optional project id used by destructive local validation scripts
  "project_id": null,
  // Log level: DEBUG / INFO / WARNING / ERROR / CRITICAL
  "log_level": "INFO"
}
"""


@dataclass(frozen=True)
class AppConfig:
    base_url: str
    email: str
    password: str
    timeout_seconds: int
    allow_insecure_http: bool
    project_id: str | None
    log_level: LogLevel


def _strip_json_comments(text: str) -> str:
    lines = text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        line = re.sub(r"([,\]}]\s*)//.*$", r"\1", line)
        result.append(line)
    return "\n".join(result)


def _ensure_config_file() -> None:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(TEMPLATE, encoding="utf-8")
        raise SystemExit(
            f"Config file created at {CONFIG_FILE}. "
            "Please edit it with your credentials and restart the server."
        )


def load_config() -> AppConfig:
    _ensure_config_file()

    raw = CONFIG_FILE.read_text(encoding="utf-8")
    clean = _strip_json_comments(raw)
    try:
        data = json.loads(clean)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {CONFIG_FILE}: {exc}") from exc

    base_url = data.get("base_url", "").rstrip("/")
    if not base_url:
        raise RuntimeError(f"Missing required field 'base_url' in {CONFIG_FILE}")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise RuntimeError("base_url must start with http:// or https://")

    email = data.get("email", "")
    if not email:
        raise RuntimeError(f"Missing required field 'email' in {CONFIG_FILE}")

    password = data.get("password", "")
    if not password:
        raise RuntimeError(f"Missing required field 'password' in {CONFIG_FILE}")

    if base_url.startswith("http://") and not data.get("allow_insecure_http", False):
        raise RuntimeError(
            "You are using an http:// URL. Set 'allow_insecure_http' to true in "
            f"{CONFIG_FILE} to proceed."
        )

    raw_project_id = data.get("project_id")
    project_id = None
    if raw_project_id is not None:
        if not isinstance(raw_project_id, str):
            raise RuntimeError("project_id must be a string or null")
        project_id = raw_project_id.strip() or None
        if project_id is not None:
            project_id = validate_project_id(project_id)

    timeout_seconds = data.get("timeout_seconds", 15)
    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise RuntimeError("timeout_seconds must be an integer >= 1")

    log_level = data.get("log_level", "INFO").upper()
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in valid_levels:
        raise RuntimeError(f"Invalid log_level: {log_level!r}. Must be one of {valid_levels}")
    typed_log_level = cast(LogLevel, log_level)

    return AppConfig(
        base_url=base_url,
        email=email,
        password=password,
        timeout_seconds=timeout_seconds,
        allow_insecure_http=bool(data.get("allow_insecure_http", False)),
        project_id=project_id,
        log_level=typed_log_level,
    )
