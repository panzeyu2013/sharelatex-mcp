"""Custom exception hierarchy for sharelatex-mcp.

All exceptions used across diff_engine, doc_editor, realtime, and server modules
are defined here to avoid circular imports and ensure consistent error handling.
"""

from __future__ import annotations


class SharelatexError(RuntimeError):
    """Base class for all sharelatex-mcp exceptions."""

    pass


class WebSocketError(SharelatexError):
    """WebSocket communication failure.

    Raised for connection failures, protocol errors, unexpected disconnects,
    and other realtime.py network faults.  This base class allows upper layers
    to catch all WebSocket-related errors with a single except clause.
    """

    pass


class WebSocketTimeoutError(WebSocketError):
    """WebSocket operation timed out (joinDoc, applyOtUpdate, etc.)."""

    pass


class ProjectFileNotFoundError(SharelatexError):
    """Requested file path does not exist in the project.

    Distinguished from builtins.FileNotFoundError to avoid accidental catches.
    """

    pass


class EditMatchError(SharelatexError):
    """edit.old string matched zero or multiple locations.

    Attributes:
        message: Human-readable description.
        edit_index: Optional index of the failing edit in the edits list.
        edit: Optional dict containing the failing edit.
    """

    def __init__(
        self,
        message: str,
        *,
        edit_index: int | None = None,
        edit: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.edit_index = edit_index
        self.edit = edit


class OTConflictError(SharelatexError):
    """OT version conflict — all retries exhausted in join_doc_write."""

    pass


class FileTypeError(SharelatexError):
    """Operation attempted on wrong entity type (e.g. read on fileRef)."""

    pass


class FileSizeError(SharelatexError):
    """File exceeds the configured maximum size."""

    pass


class FileReadError(SharelatexError):
    """Both WebSocket and HTTP read paths are unavailable."""

    pass


class ParamValidationError(SharelatexError):
    """Invalid tool parameter (negative offset, empty edits, oversized inputs, etc.)."""

    pass


class AuthenticationError(SharelatexError):
    """Session expired, CSRF token invalid, or 403 forbidden."""

    pass


class CacheConsistencyError(SharelatexError):
    """In-memory cache is out of sync with server state."""

    pass
