from __future__ import annotations

import re
from urllib.parse import urlsplit

_OBJECT_ID_RE = re.compile(r"^[a-fA-F0-9]{24}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

_ENTITY_TYPE_TO_PATH = {
    "doc": "doc",
    "folder": "folder",
    "file": "file",
    "fileRef": "file",
}


def validate_project_id(project_id: str) -> str:
    if not _OBJECT_ID_RE.fullmatch(project_id):
        raise RuntimeError("project_id must be a 24-character hexadecimal ObjectId")
    return project_id


def validate_entity_id(entity_id: str, field_name: str = "entity_id") -> str:
    if not _OBJECT_ID_RE.fullmatch(entity_id):
        raise RuntimeError(f"{field_name} must be a 24-character hexadecimal ObjectId")
    return entity_id


def validate_path_segment(value: str, field_name: str) -> str:
    if not value or value in {".", ".."} or not _PATH_SEGMENT_RE.fullmatch(value):
        raise RuntimeError(f"{field_name} contains unsafe URL path characters")
    return value


def normalize_entity_type(entity_type: str) -> str:
    try:
        return _ENTITY_TYPE_TO_PATH[entity_type]
    except KeyError as exc:
        allowed = ", ".join(sorted(_ENTITY_TYPE_TO_PATH))
        raise RuntimeError(f"entity_type must be one of: {allowed}") from exc


def validate_http_path(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        raise RuntimeError("HTTP path must be relative to the configured Overleaf origin")
    if "\\" in parsed.path:
        raise RuntimeError("HTTP path must not contain backslashes")
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        raise RuntimeError("HTTP path must not contain dot segments")
    return path
