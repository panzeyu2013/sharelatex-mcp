from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import websocket

from sharelatex_mcp.config import AppConfig
from sharelatex_mcp.session import OverleafSessionManager

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
        self.project_id = project_id
        self.ws: websocket.WebSocket | None = None

    def __enter__(self) -> LegacySocketConnection:
        self.session_manager.ensure_logged_in()
        logger.debug("Performing socket.io handshake for project %s", self.project_id)
        handshake = self.session_manager.http.get(f"/socket.io/1/?projectId={self.project_id}")
        if handshake.status_code != 200:
            raise RuntimeError(f"socket.io handshake failed, status code: {handshake.status_code}")

        session_id = handshake.text.split(":", 1)[0]
        parsed = urlparse(self.config.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = f"{scheme}://{parsed.netloc}/socket.io/1/websocket/{session_id}?projectId={self.project_id}"
        cookie_header = "; ".join(
            f"{cookie.name}={cookie.value}"
            for cookie in self.session_manager.http.session.cookies
        )

        logger.debug("Opening WebSocket to %s", ws_url)
        self.ws = websocket.create_connection(
            ws_url,
            header=[
                f"Cookie: {cookie_header}",
                f"Origin: {parsed.scheme}://{parsed.netloc}",
            ],
            timeout=self.config.timeout_seconds,
        )
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

    def recv(self) -> str:
        if self.ws is None:
            raise RuntimeError("WebSocket not connected")
        while True:
            try:
                message = self.ws.recv()
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
            except websocket.WebSocketConnectionClosedException as exc:
                raise RuntimeError(
                    f"WebSocket connection closed unexpectedly for project {self.project_id}"
                ) from exc
            if message == _HEARTBEAT:
                self.ws.send(_HEARTBEAT)
                continue
            return message

    def send_event_with_ack(self, ack_id: int, event_name: str, args: list[Any]) -> None:
        if self.ws is None:
            raise RuntimeError("WebSocket not connected")
        payload = f"5:{ack_id}+::" + json.dumps(
            {"name": event_name, "args": args},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self.ws.send(payload)

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

        raise RuntimeError("Failed to receive joinProjectResponse from websocket")

    def join_doc_and_apply_ot(
        self,
        project_id: str,
        doc_id: str,
        operations: list[dict[str, Any]],
    ) -> DocJoinData:
        logger.info(
            "Joining doc %s and applying OT in project %s (single connection)",
            doc_id, project_id,
        )
        with LegacySocketConnection(self.config, self.session_manager, project_id) as connection:
            connection.drain_initial_messages(2)
            connection.send_event_with_ack(
                ack_id=1,
                event_name="joinDoc",
                args=[doc_id, {"encodeRanges": True, "supportsHistoryOT": True}],
            )

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
                    raise RuntimeError("joinDoc returned unexpected structure")

                doc_data = DocJoinData(
                    snapshot_lines=payload[1],
                    version=payload[2],
                    ranges=payload[3],
                    comments=payload[4],
                    ot_type=payload[5],
                )
                logger.debug("Received joinDoc ack for doc %s, version=%s", doc_id, doc_data.version)
                break
            else:
                raise RuntimeError("Failed to receive joinDoc ack response")

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

            for _ in range(_MAX_DRAIN_ITER):
                message = connection.recv()
                if message.startswith("5:::"):
                    try:
                        payload = json.loads(message[4:])
                    except json.JSONDecodeError:
                        logger.warning("Unparseable applyOtUpdate response")
                        continue
                    event_name = payload.get("name")
                    args = payload.get("args", [])
                    if event_name == "otUpdateApplied" and args:
                        update = args[0]
                        if update.get("doc") == doc_id:
                            logger.debug("OT update applied to doc %s", doc_id)
                            return doc_data
                    if event_name == "otUpdateError":
                        raise RuntimeError(f"applyOtUpdate failed: {payload}")
                if message.startswith("6:::2+"):
                    try:
                        payload = json.loads(message.split("+", 1)[1])
                    except json.JSONDecodeError:
                        logger.warning("Unparseable applyOtUpdate ack response")
                        continue
                    if payload and payload[0] is not None:
                        raise RuntimeError(f"applyOtUpdate returned error: {payload[0]}")
                if "otUpdateError" in message:
                    raise RuntimeError(f"applyOtUpdate failed: {message}")

        raise RuntimeError("Failed to receive applyOtUpdate confirmation")
