from __future__ import annotations

import contextlib
import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import websocket

from sharelatex_mcp.config import AppConfig
from sharelatex_mcp.errors import (
    OTConflictError,
    OTTransportError,
    WebSocketError,
    WebSocketTimeoutError,
)
from sharelatex_mcp.jobs import ProgressCallback
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

# socket.io v0.9 servers expect a heartbeat reply roughly every heartbeat
# interval. recv() waits are kept at least this long so the inline heartbeat
# reply in recv() can keep long snapshot transfers / ack waits alive.
_HEARTBEAT_INTERVAL_SECONDS = 25

# Retry configuration
_OT_MAX_RETRIES = 2
_OT_BASE_DELAY = 0.1   # 100 ms


def _join_snapshot_lines(snapshot_lines: Iterable[str]) -> str:
    """Join Overleaf snapshot lines into doc text, normalizing CRLF to LF.

    Stripping a trailing ``\\r`` per line makes the text identical across the
    WebSocket snapshot and the HTTP-fallback read, so ``read``/``write``/``edit``
    all operate on the same representation.
    """
    return "\n".join(line.rstrip("\r") for line in snapshot_lines)


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
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise WebSocketError("socket.io handshake returned an invalid session id")
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
            try:
                self.ws.send(data)
            except websocket.WebSocketException as exc:
                raise WebSocketError(f"Failed to send data: {exc}") from exc

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

    def drain_initial_messages(self, expected_count: int = 1) -> None:
        # socket.io v0.9 sends exactly one connect frame ("1::") on open; nothing
        # else arrives until the first heartbeat (~25s), so draining more than one
        # frame would block every operation for that long.
        for i in range(expected_count):
            message = self.recv()
            if message == _CONNECT_ACK:
                logger.debug("Received socket.io connect ack (%d/%d)", i + 1, expected_count)
            elif message.startswith("5:::"):
                try:
                    payload = json.loads(message[4:])
                    if isinstance(payload, dict):
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

                if not isinstance(payload, dict) or payload.get("name") != "joinProjectResponse":
                    continue

                args = payload.get("args", [])
                if not isinstance(args, list) or not args:
                    break

                response = args[0]
                if not isinstance(response, dict):
                    break
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
            connection.drain_initial_messages()
            connection.send_event_with_ack(
                ack_id=1,
                event_name="joinDoc",
                args=[doc_id, {"encodeRanges": True, "supportsHistoryOT": True}],
            )
            doc_data = self._receive_join_doc_ack(connection, doc_id)
        return _join_snapshot_lines(doc_data.snapshot_lines)

    def join_doc_write(
        self,
        project_id: str,
        doc_id: str,
        diff_fn: Callable[[str], list[dict[str, Any]]],
        timeout: float | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        """Single-connection write/edit: joinDoc → diff_fn(content) → applyOtUpdate.

        The entire read-modify-write cycle happens inside one WebSocket
        lifetime, eliminating TOCTOU between the read and the OT submission.

        *diff_fn* receives the raw document content and must return a list
        of OT operations.  If it returns ``[]`` the OT round-trip is
        skipped entirely.

        *timeout* bounds the total wall-clock for all attempts in seconds; when
        omitted it is derived from ``config.timeout_seconds``.  Every attempt
        (including retries) receives the remaining budget, so the worst case is
        bounded exactly while a slow-but-alive server is never starved.

        *progress* is an optional ``(done, total, message)`` callback fired at
        coarse pipeline stages (snapshot → diff → send → ack).

        On OT version conflict, automatically re-joins and retries up to
        ``_OT_MAX_RETRIES`` times with exponential backoff.

        Raises ``OTConflictError`` if all retries are exhausted.
        """
        budget = timeout if timeout is not None else self._ack_budget()
        deadline = time.monotonic() + budget
        last_exc: OTConflictError | OTTransportError | None = None
        for attempt in range(_OT_MAX_RETRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self._join_doc_write_once(project_id, doc_id, diff_fn, remaining, progress)
                return
            except (OTConflictError, OTTransportError) as exc:
                last_exc = exc
                if attempt < _OT_MAX_RETRIES and time.monotonic() < deadline:
                    delay = min(
                        _OT_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.05),
                        max(0.0, deadline - time.monotonic()),
                    )
                    logger.warning(
                        "OT %s on doc %s (attempt %d/%d), retrying in %.2fs",
                        "conflict" if isinstance(exc, OTConflictError) else "transport error",
                        doc_id, attempt + 1, _OT_MAX_RETRIES + 1, delay,
                    )
                    time.sleep(delay)
                else:
                    raise
        if last_exc is not None:
            raise last_exc
        raise OTConflictError(
            f"OT write did not complete within {budget:.0f}s on doc {doc_id}"
        )

    def _ack_budget(self) -> float:
        """Overall deadline for a single joinDoc/ack attempt.

        Derived from ``config.timeout_seconds`` so raising the configured
        timeout also extends the realtime-phase deadline on slow links.
        """
        return max(30.0, float(self.config.timeout_seconds))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _join_doc_write_once(
        self,
        project_id: str,
        doc_id: str,
        diff_fn: Callable[[str], list[dict[str, Any]]],
        timeout: float,
        progress: ProgressCallback | None = None,
    ) -> None:
        """Execute one attempt of the joinDoc → diff → applyOtUpdate cycle.

        ``timeout`` is the total budget for this attempt; it is shared between
        the joinDoc and ack phases so the whole attempt is bounded by it.
        """
        attempt_deadline = time.monotonic() + timeout
        with LegacySocketConnection(self.config, self.session_manager, project_id) as connection:
            try:
                connection.drain_initial_messages()
                connection.send_event_with_ack(
                    ack_id=1,
                    event_name="joinDoc",
                    args=[doc_id, {"encodeRanges": True, "supportsHistoryOT": True}],
                )
                doc_data = self._receive_join_doc_ack(
                    connection,
                    doc_id,
                    timeout=max(0.0, attempt_deadline - time.monotonic()),
                )
                current = _join_snapshot_lines(doc_data.snapshot_lines)
                if progress is not None:
                    progress(1, 4, "Snapshot loaded")

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
                    if progress is not None:
                        progress(2, 4, "Diff computed")
                        progress(4, 4, "Update acknowledged")
                    return
                if progress is not None:
                    progress(2, 4, "Diff computed")

                # _receive_join_doc_ack leaves a per-recv socket timeout that may
                # be smaller than needed to push out a large OT payload; reset it
                # so a slow send is bounded by the remaining attempt budget
                # instead of failing early and forcing a wasteful re-join retry.
                if connection.ws is not None:
                    with contextlib.suppress(Exception):
                        connection.ws.settimeout(
                            min(
                                self.config.timeout_seconds,
                                max(0.0, attempt_deadline - time.monotonic()),
                            )
                        )

                payload_chars = sum(
                    len(op.get("d", "")) + len(op.get("i", ""))
                    for op in operations
                )
                logger.debug(
                    "Sending applyOtUpdate for doc %s (%d ops, ~%.1f KB)",
                    doc_id, len(operations), payload_chars / 1024,
                )
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
                if progress is not None:
                    progress(3, 4, "Update sent")

                self._wait_for_ack(
                    connection,
                    ack_id=2,
                    doc_id=doc_id,
                    timeout=max(0.0, attempt_deadline - time.monotonic()),
                )
                if progress is not None:
                    progress(4, 4, "Update acknowledged")
            except (WebSocketError, WebSocketTimeoutError) as exc:
                raise OTTransportError(str(exc)) from exc

    def _receive_join_doc_ack(
        self,
        connection: LegacySocketConnection,
        doc_id: str,
        timeout: float | None = None,
    ) -> DocJoinData:
        """Wait for and parse the joinDoc ack response (``6:::1+[...]``).

        Uses a recv timeout at least as long as the socket.io heartbeat
        interval so ``recv()`` can answer server heartbeats inline while a
        large snapshot is being transferred, keeping the connection alive.

        *timeout* bounds the overall wait; when omitted it is derived from
        ``config.timeout_seconds``.
        """
        timeout = timeout if timeout is not None else self._ack_budget()
        wait_granularity = max(
            self.config.timeout_seconds, _HEARTBEAT_INTERVAL_SECONDS + 5
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if connection.ws is not None:
                with contextlib.suppress(Exception):
                    connection.ws.settimeout(min(remaining, wait_granularity))
            message = connection.recv()
            if not message.startswith("6:::1+"):
                continue

            try:
                payload = json.loads(message.split("+", 1)[1])
            except json.JSONDecodeError:
                logger.warning("Unparseable joinDoc ack response")
                continue

            if not isinstance(payload, list) or len(payload) < 6:
                raise WebSocketError("joinDoc returned unexpected structure")
            if payload[0] is not None:
                raise WebSocketError(f"joinDoc error: {payload[0]}")

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
        # Wait long enough per recv() to catch socket.io heartbeats so the
        # inline reply in recv() keeps the connection alive while we wait.
        wait_granularity = max(
            self.config.timeout_seconds, _HEARTBEAT_INTERVAL_SECONDS + 5
        )

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Set per-recv timeout to respect the overall deadline
            try:
                if connection.ws is not None:
                    connection.ws.settimeout(min(remaining, wait_granularity))
            except Exception:
                pass
            try:
                message = connection.recv()
            except WebSocketTimeoutError:
                continue  # still waiting — re-check deadline and retry
            except WebSocketError as exc:
                # Connection dropped: fail fast instead of burning the
                # remaining budget on a dead socket.
                raise OTTransportError(
                    f"WebSocket connection lost while waiting for ack {ack_id} on doc {doc_id}"
                ) from exc

            if message.startswith("5:::"):
                try:
                    parsed = json.loads(message[4:])
                except json.JSONDecodeError:
                    continue

                if not isinstance(parsed, dict):
                    continue

                event_name = parsed.get("name")
                if event_name == "otUpdateError":
                    args = parsed.get("args", [])
                    if isinstance(args, list) and args and isinstance(args[0], dict) and args[0].get("doc") == doc_id:
                        raise OTConflictError(f"applyOtUpdate error from server: {parsed}")
                # Ignore otUpdateApplied broadcasts — they are not our ack
                continue

            if message.startswith(ack_prefix):
                try:
                    payload = json.loads(message.split("+", 1)[1])
                except json.JSONDecodeError:
                    logger.warning("Unparseable applyOtUpdate ack response")
                    continue
                if not isinstance(payload, list):
                    raise OTTransportError(
                        f"Unexpected applyOtUpdate ack payload for doc {doc_id}: {payload!r}"
                    )
                if payload and payload[0] is not None:
                    raise OTConflictError(f"applyOtUpdate returned error: {payload[0]}")
                logger.debug("OT update acknowledged for doc %s (ack_id=%d)", doc_id, ack_id)
                return

        raise WebSocketTimeoutError(
            f"Timed out waiting for ack {ack_id} on doc {doc_id}"
        )
