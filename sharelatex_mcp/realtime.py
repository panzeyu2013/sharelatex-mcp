from __future__ import annotations

import contextlib
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import websocket

from sharelatex_mcp.config import AppConfig
from sharelatex_mcp.errors import (
    OTConflictError,
    WebSocketError,
    WebSocketTimeoutError,
)
from sharelatex_mcp.session import OverleafSessionManager
from sharelatex_mcp.validation import validate_project_id

logger = logging.getLogger(__name__)


@dataclass
class ProjectJoinData:
    project: dict[str, Any]
    permissions_level: str | None
    protocol_version: int | None
    public_id: str | None


@dataclass
class DocJoinData:
    snapshot_lines: list[str]
    version: int
    ranges: list[Any]
    comments: dict[str, Any]
    ot_type: str


_CONNECT_ACK = "1::"
_HEARTBEAT = "2::"
_MAX_DRAIN_ITER = 20

# Retry configuration
_OT_MAX_RETRIES = 3
_OT_BASE_DELAY = 0.1   # 100 ms


class LegacySocketConnection:
    """Manages a single WebSocket connection to a legacy socket.io v0.9 endpoint.

    Message framing (socket.io protocol v0.9):
      0     disconnect
      1     connect
      2     heartbeat (server ping → client must reply "2::")
      3     message (unused here)
      4     JSON message (unused here)
      5     JSON event with optional ack id: "5:{ack_id}+::" + JSON payload
      6     ack response: "6:::{ack_id}+" + JSON payload
      7     error
      8     noop

    We only care about 2 (heartbeat), 5 (events from server), and 6 (ack responses).
    """

    def __init__(self, config: AppConfig, session_manager: OverleafSessionManager, project_id: str) -> None:
        self.config = config
        self.session_manager = session_manager
        self.project_id = validate_project_id(project_id)
        self.ws: websocket.WebSocket | None = None
        self._send_lock = threading.Lock()

    def __enter__(self) -> LegacySocketConnection:
        self.session_manager.ensure_logged_in()
        logger.debug("Performing socket.io handshake for project %s", self.project_id)
        handshake = self.session_manager.http.get(f"/socket.io/1/?projectId={self.project_id}")
        if handshake.status_code != 200:
            raise WebSocketError(f"socket.io handshake failed, status code: {handshake.status_code}")

        session_id = handshake.text.split(":", 1)[0]
        parsed = urlparse(self.config.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = f"{scheme}://{parsed.netloc}/socket.io/1/websocket/{session_id}?projectId={self.project_id}"
        cookie_header = "; ".join(
            f"{cookie.name}={cookie.value}"
            for cookie in self.session_manager.http.session.cookies
        )

        logger.debug("Opening WebSocket to %s", ws_url)
        try:
            self.ws = websocket.create_connection(
                ws_url,
                header=[
                    f"Cookie: {cookie_header}",
                    f"Origin: {parsed.scheme}://{parsed.netloc}",
                ],
                timeout=self.config.timeout_seconds,
            )
        except Exception as exc:
            raise WebSocketError(f"Failed to open WebSocket connection: {exc}") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                logger.debug("Error closing WebSocket for project %s", self.project_id, exc_info=True)
            finally:
                self.ws = None
                logger.debug("WebSocket closed for project %s", self.project_id)

    def _send_locked(self, data: str) -> None:
        """Thread-safe send — guards against concurrent heartbeat sends."""
        if self.ws is None:
            raise WebSocketError("WebSocket not connected")
        with self._send_lock:
            self.ws.send(data)

    def recv(self) -> str:
        if self.ws is None:
            raise WebSocketError("WebSocket not connected")
        while True:
            try:
                message = self.ws.recv()
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
            except websocket.WebSocketConnectionClosedException as exc:
                raise WebSocketError(
                    f"WebSocket connection closed unexpectedly for project {self.project_id}"
                ) from exc
            except websocket.WebSocketTimeoutException as exc:
                raise WebSocketTimeoutError(
                    f"WebSocket receive timed out for project {self.project_id}"
                ) from exc
            if message == _HEARTBEAT:
                with contextlib.suppress(Exception):
                    self._send_locked(_HEARTBEAT)
                continue
            return message

    def send_event_with_ack(self, ack_id: int, event_name: str, args: list[Any]) -> None:
        payload = f"5:{ack_id}+::" + json.dumps(
            {"name": event_name, "args": args},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._send_locked(payload)

    def drain_initial_messages(self, expected_count: int = 2) -> None:
        for i in range(expected_count):
            message = self.recv()
            if message == _CONNECT_ACK:
                logger.debug("Received socket.io connect ack (%d/%d)", i + 1, expected_count)
            elif message.startswith("5:::"):
                try:
                    payload = json.loads(message[4:])
                    logger.debug(
                        "Received server event '%s' during drain (%d/%d)",
                        payload.get("name", "?"), i + 1, expected_count,
                    )
                except json.JSONDecodeError:
                    logger.warning("Unparseable server event during drain (%d/%d)", i + 1, expected_count)
            else:
                logger.debug(
                    "Draining initial message (%d/%d): %s",
                    i + 1, expected_count, message[:80],
                )


class RealtimeProjectClient:
    def __init__(self, config: AppConfig, session_manager: OverleafSessionManager) -> None:
        self.config = config
        self.session_manager = session_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def join_project(self, project_id: str) -> ProjectJoinData:
        logger.info("Joining project %s via realtime socket", project_id)
        with LegacySocketConnection(self.config, self.session_manager, project_id) as connection:
            for _ in range(_MAX_DRAIN_ITER):
                message = connection.recv()
                if not message.startswith("5:::"):
                    continue

                try:
                    payload = json.loads(message[4:])
                except json.JSONDecodeError:
                    logger.warning("Unparseable server event in join_project")
                    continue

                if payload.get("name") != "joinProjectResponse":
                    continue

                args = payload.get("args", [])
                if not args:
                    break

                response = args[0]
                logger.debug("Received joinProjectResponse for project %s", project_id)
                return ProjectJoinData(
                    project=response.get("project", {}),
                    permissions_level=response.get("permissionsLevel"),
                    protocol_version=response.get("protocolVersion"),
                    public_id=response.get("publicId"),
                )

        raise WebSocketError("Failed to receive joinProjectResponse from websocket")

    def join_doc_read(self, project_id: str, doc_id: str) -> str:
        """Single-connection read: joinDoc → snapshot_lines → return full text.

        Used by ``read()``.  Returns the raw document content as a single
        string (lines joined with ``\\n``).

        Raises ``WebSocketError`` on any failure — caller should fall back
        to HTTP download.
        """
        logger.info("Reading doc %s via WebSocket joinDoc (project %s)", doc_id, project_id)
        with LegacySocketConnection(self.config, self.session_manager, project_id) as connection:
            connection.drain_initial_messages(2)
            connection.send_event_with_ack(
                ack_id=1,
                event_name="joinDoc",
                args=[doc_id, {"encodeRanges": True, "supportsHistoryOT": True}],
            )
            doc_data = self._receive_join_doc_ack(connection, doc_id)
        return "\n".join(doc_data.snapshot_lines)

    def join_doc_write(
        self,
        project_id: str,
        doc_id: str,
        diff_fn: Callable[[str], list[dict[str, Any]]],
        timeout: float = 30.0,
    ) -> None:
        """Single-connection write/edit: joinDoc → diff_fn(content) → applyOtUpdate.

        The entire read-modify-write cycle happens inside one WebSocket
        lifetime, eliminating TOCTOU between the read and the OT submission.

        *diff_fn* receives the raw document content and must return a list
        of OT operations.  If it returns ``[]`` the OT round-trip is
        skipped entirely.

        On OT version conflict, automatically re-joins and retries up to
        ``_OT_MAX_RETRIES`` times with exponential backoff.

        Raises ``OTConflictError`` if all retries are exhausted.
        """
        for attempt in range(_OT_MAX_RETRIES + 1):
            try:
                self._join_doc_write_once(project_id, doc_id, diff_fn, timeout)
                return
            except OTConflictError:
                if attempt < _OT_MAX_RETRIES:
                    delay = _OT_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.05)
                    logger.warning(
                        "OT conflict on doc %s (attempt %d/%d), retrying in %.2fs",
                        doc_id, attempt + 1, _OT_MAX_RETRIES + 1, delay,
                    )
                    time.sleep(delay)
                else:
                    raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _join_doc_write_once(
        self,
        project_id: str,
        doc_id: str,
        diff_fn: Callable[[str], list[dict[str, Any]]],
        timeout: float,
    ) -> None:
        """Execute one attempt of the joinDoc → diff → applyOtUpdate cycle."""
        with LegacySocketConnection(self.config, self.session_manager, project_id) as connection:
            connection.drain_initial_messages(2)
            connection.send_event_with_ack(
                ack_id=1,
                event_name="joinDoc",
                args=[doc_id, {"encodeRanges": True, "supportsHistoryOT": True}],
            )
            doc_data = self._receive_join_doc_ack(connection, doc_id)
            current = "\n".join(doc_data.snapshot_lines)

            # Start heartbeat thread before calling diff_fn
            heartbeat_stop = threading.Event()

            def _heartbeat_loop() -> None:
                while not heartbeat_stop.wait(timeout=10):
                    try:
                        connection._send_locked(_HEARTBEAT)
                    except Exception:
                        break

            heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
            heartbeat_thread.start()

            try:
                operations = diff_fn(current)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=2)

            if not operations:
                logger.debug("diff_fn returned empty operations for doc %s, skipping OT", doc_id)
                return

            connection.send_event_with_ack(
                ack_id=2,
                event_name="applyOtUpdate",
                args=[
                    doc_id,
                    {
                        "doc": doc_id,
                        "op": operations,
                        "v": doc_data.version,
                    },
                ],
            )

            try:
                self._wait_for_ack(connection, ack_id=2, doc_id=doc_id, timeout=timeout)
            except (WebSocketError, WebSocketTimeoutError) as exc:
                # Could be transient; let the outer retry loop handle it
                raise OTConflictError(f"applyOtUpdate failed: {exc}") from exc

    def _receive_join_doc_ack(self, connection: LegacySocketConnection, doc_id: str) -> DocJoinData:
        """Wait for and parse the joinDoc ack response (``6:::1+[...]``)."""
        for _ in range(_MAX_DRAIN_ITER):
            message = connection.recv()
            if not message.startswith("6:::1+"):
                continue

            try:
                payload = json.loads(message.split("+", 1)[1])
            except json.JSONDecodeError:
                logger.warning("Unparseable joinDoc ack response")
                continue

            if len(payload) < 6:
                raise WebSocketError("joinDoc returned unexpected structure")

            doc_data = DocJoinData(
                snapshot_lines=payload[1],
                version=payload[2],
                ranges=payload[3],
                comments=payload[4],
                ot_type=payload[5],
            )
            logger.debug("Received joinDoc ack for doc %s, version=%s", doc_id, doc_data.version)
            return doc_data

        raise WebSocketError("Failed to receive joinDoc ack response")

    def _wait_for_ack(
        self,
        connection: LegacySocketConnection,
        ack_id: int,
        doc_id: str,
        timeout: float,
    ) -> None:
        """Wait for a specific ack response, ignoring broadcasts.

        Correctly distinguishes between:
        - ``6:::<ack_id>+[...]``  — direct ack to our ``applyOtUpdate``
        - ``5:::{"name":"otUpdateApplied",...}``  — broadcast to ALL joined clients
        - ``5:::{"name":"otUpdateError",...}``  — error broadcast

        The old code at realtime.py:252-256 treated ``otUpdateApplied``
        broadcasts as acks, causing premature returns under concurrent
        editing.
        """
        ack_prefix = f"6:::{ack_id}+"
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Set per-recv timeout to respect the overall deadline
            try:
                connection.ws.settimeout(min(remaining, self.config.timeout_seconds))
            except Exception:
                connection.ws.settimeout(1.0)  # safe fallback
            try:
                message = connection.recv()
            except (WebSocketTimeoutError, WebSocketError):
                continue  # re-check deadline and retry

            if message.startswith("5:::"):
                try:
                    payload = json.loads(message[4:])
                except json.JSONDecodeError:
                    continue

                event_name = payload.get("name")
                if event_name == "otUpdateError":
                    args = payload.get("args", [])
                    if args and args[0].get("doc") == doc_id:
                        raise OTConflictError(f"applyOtUpdate error from server: {payload}")
                # Ignore otUpdateApplied broadcasts — they are not our ack
                continue

            if message.startswith(ack_prefix):
                try:
                    payload = json.loads(message.split("+", 1)[1])
                except json.JSONDecodeError:
                    logger.warning("Unparseable applyOtUpdate ack response")
                    continue
                if payload and payload[0] is not None:
                    raise OTConflictError(f"applyOtUpdate returned error: {payload[0]}")
                logger.debug("OT update acknowledged for doc %s (ack_id=%d)", doc_id, ack_id)
                return

        raise WebSocketTimeoutError(
            f"Timed out waiting for ack {ack_id} on doc {doc_id}"
        )
