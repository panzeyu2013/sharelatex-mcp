"""Document editor — read, write, and edit orchestration.

Wraps ProjectClient's entity-resolution, cache, and HTTP layer with
WebSocket-first read/write/edit primitives as described in the design doc.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from sharelatex_mcp.diff_engine import (
    MAX_EDITS_PER_CALL,
    MAX_FILE_SIZE,
    MAX_NEW_LENGTH,
    MAX_OLD_LENGTH,
    _compute_edit_operations,
    check_edits_already_applied,
    compute_diff_operations,
    convert_ot_positions_to_utf16,
)
from sharelatex_mcp.errors import (
    CacheConsistencyError,
    EditMatchError,
    FileReadError,
    FileSizeError,
    FileTypeError,
    OTConflictError,
    OTTransportError,
    ParamValidationError,
    ProjectFileNotFoundError,
    WebSocketError,
)
from sharelatex_mcp.jobs import ProgressCallback
from sharelatex_mcp.projects import _per_project_lock
from sharelatex_mcp.validation import validate_path_segment, validate_project_id

if TYPE_CHECKING:
    from sharelatex_mcp.projects import ProjectClient  # pragma: no cover

logger = logging.getLogger(__name__)

_BINARY_EXTENSIONS = frozenset({".png", ".pdf", ".jpg", ".jpeg", ".gif", ".svg", ".eps"})


class DocEditor:
    """Read, write, and edit text documents in an Overleaf project.

    Constructor receives a ``ProjectClient`` for entity resolution, HTTP,
    cache access, and the realtime client.
    """

    def __init__(self, client: ProjectClient) -> None:
        self._client = client
        self._realtime = client.realtime_client

    def _op_lock(self, project_id: str) -> Any:
        """Per-project reentrant lock, or a no-op for fakes without ``_op_lock``."""
        op_lock = getattr(self._client, "_op_lock", None)
        if callable(op_lock):
            return op_lock(project_id)
        return contextlib.nullcontext()


    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    @_per_project_lock
    def read(
        self,
        project_id: str,
        path: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Read a doc-type file with optional line-range slicing.

        Returns a dict with ``content`` (line-numbered), ``total_lines``,
        ``returned_lines``, ``source`` (``"websocket"`` or ``"http_fallback"``).
        """
        project_id = validate_project_id(project_id)
        if offset < 0:
            raise ParamValidationError("offset must be >= 0")
        if limit is not None and limit <= 0:
            raise ParamValidationError("limit must be > 0")

        entity = self._resolve_entity(project_id, path)
        if entity.type != "doc":
            raise FileTypeError(
                "read is only supported for doc-type text files. "
                "For binary files use download_file."
            )

        content, source = self._fetch_content(project_id, entity)

        # Size guard: enforce for full-file reads, warn for sliced reads
        byte_size = len(content.encode("utf-8"))
        if limit is None and byte_size > MAX_FILE_SIZE:
            raise FileSizeError(
                f"File exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit. "
                "Use offset/limit to read a slice."
            )

        # Split on "\n" only: this exactly mirrors the snapshot_lines the
        # WebSocket read joins with "\n", preserving trailing empty lines and
        # keeping line numbers consistent with how write/edit address the doc.
        lines = content.split("\n")
        # Strip a trailing "\r" from every line so CRLF documents read the same
        # through the WebSocket and the HTTP-fallback path.
        lines = [line.rstrip("\r") for line in lines]
        if not content:
            lines = []
        total = len(lines)

        if offset >= total:
            return {
                "project_id": project_id,
                "path": path,
                "type": entity.type,
                "offset": offset,
                "returned_lines": 0,
                "total_lines": total,
                "source": source,
                "content": "",
            }

        selected = lines[offset:offset + limit] if limit else lines[offset:]
        numbered = [f"{i + offset + 1}: {line}" for i, line in enumerate(selected)]

        return {
            "project_id": project_id,
            "path": path,
            "type": entity.type,
            "offset": offset,
            "returned_lines": len(selected),
            "total_lines": total,
            "source": source,
            "content": "\n".join(numbered),
        }

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    @_per_project_lock
    def write(
        self,
        project_id: str,
        path: str,
        content: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Write content to a doc.  Auto-creates the file if it does not exist.

        *progress* is an optional ``(done, total, message)`` callback forwarded
        to the realtime pipeline.

        Returns ``{"project_id", "path", "changed", "created", "message"}``.
        """
        project_id = validate_project_id(project_id)

        # Size check on input
        if len(content.encode("utf-8")) > MAX_FILE_SIZE:
            raise FileSizeError(
                f"Content exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit"
            )

        # Check for binary extension
        if self._is_binary_path(path):
            raise FileTypeError(
                f"Path '{os.path.basename(path)}' matches a binary extension. "
                "Use upload_file for binary files."
            )

        try:
            entity = self._resolve_entity(project_id, path)
        except ProjectFileNotFoundError:
            entity = None

        if entity is None:
            return self._create_doc_and_insert(project_id, path, content)

        if entity.type != "doc":
            raise FileTypeError("write is only supported for doc-type text files")

        unchanged: list[bool] = [False]
        applied_changes: list[bool] = [False]  # True if diff_fn ever produced non-empty ops

        def _diff_fn(current: str) -> list[dict[str, Any]]:
            if current == content:
                unchanged[0] = True
                return []
            applied_changes[0] = True
            ops = compute_diff_operations(current, content)
            return convert_ot_positions_to_utf16(ops, current)

        try:
            self._realtime.join_doc_write(
                project_id, entity.entity_id or "",
                _diff_fn,
                progress=progress,
            )
        except OTTransportError:
            # A transport failure means the update may or may not have been
            # applied; verify by re-reading instead of reporting a spurious error.
            try:
                content_after, _ = self._fetch_content(project_id, entity)
            except Exception:
                raise
            if content_after == content:
                logger.info("write idempotent — content already applied (lost ack recovered)")
                self._client._invalidate_caches(project_id)
                return {
                    "project_id": project_id,
                    "path": path,
                    "changed": True,
                    "created": False,
                    "message": "Write already applied (ack recovery)",
                }
            raise

        changed = applied_changes[0] or not unchanged[0]
        if changed:
            self._client._invalidate_caches(project_id)
        return {
            "project_id": project_id,
            "path": path,
            "changed": changed,
            "created": False,
            "message": "File content unchanged, skipped write" if not changed else "Write operation sent",
        }

    # ------------------------------------------------------------------
    # edit
    # ------------------------------------------------------------------

    @_per_project_lock
    def edit(
        self,
        project_id: str,
        path: str,
        edits: list[dict[str, str]],
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Apply a batch of find-and-replace edits atomically.

        Each edit is ``{"old": str, "new": str}``.  ``old`` must match
        exactly one location after prior edits are applied.

        *progress* is an optional ``(done, total, message)`` callback forwarded
        to the realtime pipeline.
        """
        project_id = validate_project_id(project_id)
        self._validate_edits(edits)

        # Only non-identity edits actually change the document
        applied_count = sum(1 for e in edits if e["old"] != e["new"])

        # All identity edits → short-circuit
        if applied_count == 0:
            return {
                "project_id": project_id,
                "path": path,
                "changed": False,
                "edits_applied": 0,
                "message": "All edits are identity (no change)",
            }

        entity = self._resolve_entity(project_id, path)
        if entity.type != "doc":
            raise FileTypeError("edit is only supported for doc-type text files")

        doc_id = entity.entity_id or ""
        expected_content: list[str | None] = [None]

        def _diff_fn(current: str) -> list[dict[str, Any]]:
            ops, modified = _compute_edit_operations(current, edits)
            if len(modified.encode("utf-8")) > MAX_FILE_SIZE:
                raise FileSizeError(
                    f"Edited content exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit"
                )
            expected_content[0] = modified
            return convert_ot_positions_to_utf16(ops, current)

        try:
            self._realtime.join_doc_write(project_id, doc_id, _diff_fn, progress=progress)
        except (OTConflictError, EditMatchError, OTTransportError) as ot_exc:
            try:
                content, _ = self._fetch_content(project_id, entity)
            except Exception:
                raise ot_exc from None  # propagate original OT conflict if we can't verify

            if check_edits_already_applied(content, expected_content[0]):
                logger.info("edit idempotent — changes already applied (lost ack recovered)")
                self._client._invalidate_caches(project_id)
                return {
                    "project_id": project_id,
                    "path": path,
                    "changed": True,
                    "edits_applied": applied_count,
                    "message": "Edits were already applied (ack recovery)",
                }
            raise

        self._client._invalidate_caches(project_id)
        return {
            "project_id": project_id,
            "path": path,
            "changed": True,
            "edits_applied": applied_count,
            "message": "Edits applied",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_entity(self, project_id: str, path: str):
        """Resolve path → ProjectEntity, raising ProjectFileNotFoundError."""
        try:
            return self._client._resolve_entity_by_path(project_id, path)
        except RuntimeError as exc:
            raise ProjectFileNotFoundError(str(exc)) from exc

    def _fetch_content(self, project_id: str, entity) -> tuple[str, str]:
        """Fetch document content. Returns (text, source).

        Primary path: WebSocket joinDoc.  Falls back to HTTP download.
        """
        doc_id = entity.entity_id
        if not doc_id:
            raise FileReadError("Entity has no document ID")

        # Primary: WebSocket
        try:
            content = self._realtime.join_doc_read(project_id, doc_id)
            return content, "websocket"
        except WebSocketError:
            logger.warning("WebSocket read failed for %s, falling back to HTTP", entity.path)

        # Fallback: HTTP
        try:
            result = self._client.session_manager.http.get(
                f"/project/{project_id}/doc/{doc_id}/download"
            )
            if result.status_code != 200:
                raise FileReadError(f"HTTP download failed, status {result.status_code}")
            # Normalize CRLF to LF so the fallback text matches the WebSocket
            # snapshot representation (see realtime._join_snapshot_lines).
            normalized = "\n".join(
                line.rstrip("\r") for line in result.text.split("\n")
            )
            return normalized, "http_fallback"
        except Exception:
            raise FileReadError("Both WebSocket and HTTP reads are unavailable") from None

    def _is_binary_path(self, path: str) -> bool:
        """Check if path has a known binary extension."""
        ext = os.path.splitext(path)[1].lower()
        return ext in _BINARY_EXTENSIONS

    def _validate_edits(self, edits: list[dict[str, str]]) -> None:
        """Validate edit list before processing (design doc §5.2)."""
        if not edits or not isinstance(edits, list):
            raise ParamValidationError("edits must be a non-empty list")
        if len(edits) > MAX_EDITS_PER_CALL:
            raise ParamValidationError(
                f"Maximum {MAX_EDITS_PER_CALL} edits per call, got {len(edits)}"
            )
        for i, e in enumerate(edits):
            if not isinstance(e, dict) or "old" not in e or "new" not in e:
                raise ParamValidationError(f"edits[{i}] missing 'old' or 'new' field")
            if not isinstance(e["old"], str) or not isinstance(e["new"], str):
                raise ParamValidationError(f"edits[{i}].old and .new must be strings")
            if len(e["old"].encode("utf-8")) > MAX_OLD_LENGTH:
                raise ParamValidationError(
                    f"edits[{i}].old exceeds {MAX_OLD_LENGTH} bytes"
                )
            if len(e["new"].encode("utf-8")) > MAX_NEW_LENGTH:
                raise ParamValidationError(
                    f"edits[{i}].new exceeds {MAX_NEW_LENGTH} bytes"
                )
            if e["old"].strip() == "":
                raise ParamValidationError(
                    f"edits[{i}].old must not be empty or whitespace-only"
                )

    def _create_doc_and_insert(
        self, project_id: str, path: str, content: str
    ) -> dict[str, Any]:
        """Create a new doc and write *content* into it.

        Rollback: if WebSocket insert fails after HTTP create succeeds,
        tries to delete the orphaned empty document.

        Returns ``{"project_id", "path", "changed": true, "created": true, "message"}``.
        """
        # Extract parent folder path and filename from the target path
        parent_path = os.path.dirname(path) or "/"
        name = os.path.basename(path)
        if not name:
            raise ParamValidationError("path must include a filename")

        # Normalize before resolving: entity paths are stored with a leading "/"
        parent_path = parent_path.rstrip("/") or "/"
        if not parent_path.startswith("/"):
            parent_path = "/" + parent_path

        # Resolve parent folder
        try:
            parent_folder_id, _ = self._client._resolve_folder_id_by_path(
                project_id, parent_path
            )
        except RuntimeError as exc:
            raise ProjectFileNotFoundError(
                f"Parent folder not found: {parent_path}"
            ) from exc

        logger.info(
            "Creating doc '%s' in project %s (parent: %s)", name, project_id, parent_folder_id
        )
        result = self._client._post_json_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/doc",
            payload={"parent_folder_id": parent_folder_id, "name": name},
            extra_headers={"Accept": "application/json"},
        )
        if not 200 <= result.status_code < 300:
            raise RuntimeError(f"Failed to create document, status code: {result.status_code}")

        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Failed to create document: invalid JSON response") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Failed to create document: unexpected response format")

        entity_id = payload.get("_id")
        if not entity_id:
            raise RuntimeError("Document created but no document ID returned")

        entity_id = validate_path_segment(entity_id, "doc_id")
        doc_path = f"{parent_path}/{name}" if parent_path != "/" else f"/{name}"

        # Write content via WebSocket first — only cache on success
        try:
            def _diff_fn(current: str) -> list[dict[str, Any]]:
                ops = compute_diff_operations(current, content)
                return convert_ot_positions_to_utf16(ops, current)

            self._realtime.join_doc_write(project_id, entity_id, _diff_fn)
        except Exception as write_exc:
            logger.error(
                "WebSocket write failed after creating doc %s, attempting rollback", doc_path
            )
            # Rollback: delete the empty doc
            try:
                from sharelatex_mcp.validation import normalize_entity_type
                mapped_type = normalize_entity_type("doc")
                delete_result = self._client._delete_with_csrf(
                    project_id=project_id,
                    path=f"/project/{project_id}/{mapped_type}/{entity_id}",
                )
                if not 200 <= delete_result.status_code < 300:
                    raise RuntimeError(f"Delete returned status {delete_result.status_code}")
                self._client._invalidate_caches(project_id)
                logger.info("Rollback succeeded — deleted orphaned doc %s", doc_path)
            except Exception as rollback_exc:
                logger.error(
                    "Rollback failed for doc %s: %s. Cache may be stale.",
                    doc_path, rollback_exc,
                )
                raise CacheConsistencyError(
                    "Document created but write failed and rollback also failed. "
                    "Run list_files to refresh state."
                ) from write_exc
            raise

        # Write succeeded — now update cache
        from sharelatex_mcp.projects import ProjectEntity
        self._client._cache_upsert(
            project_id,
            ProjectEntity(
                path=doc_path,
                type="doc",
                entity_id=entity_id,
                parent_folder_id=parent_folder_id,
            ),
        )
        self._client._invalidate_caches(project_id)
        return {
            "project_id": project_id,
            "path": doc_path,
            "changed": True,
            "created": True,
            "message": "Document created and content written",
        }
