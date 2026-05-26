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
                f"Origin: {self.config.base_url}",
            ],
            timeout=self.config.timeout_seconds,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None
            logger.debug("WebSocket closed for project %s", self.project_id)

    def recv(self) -> str:
        if self.ws is None:
            raise RuntimeError("WebSocket not connected")
        message = self.ws.recv()
        if message == "2::":
            self.ws.send("2::")
            return self.recv()
        return message

    def send_event_with_ack(self, ack_id: int, event_name: str, args: list[Any]) -> None:
        """Send a socket.io event expecting an ack response.

        Frame format: "5:{ack_id}+::" + JSON({name, args})
        """
        if self.ws is None:
            raise RuntimeError("WebSocket not connected")
        payload = f"5:{ack_id}+::" + json.dumps(
            {"name": event_name, "args": args},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self.ws.send(payload)


class RealtimeProjectClient:
    def __init__(self, config: AppConfig, session_manager: OverleafSessionManager) -> None:
        self.config = config
        self.session_manager = session_manager

    def join_project(self, project_id: str) -> ProjectJoinData:
        logger.info("Joining project %s via realtime socket", project_id)
        with LegacySocketConnection(self.config, self.session_manager, project_id) as connection:
            for _ in range(20):
                message = connection.recv()
                if not message.startswith("5:::"):
                    continue

                payload = json.loads(message[4:])
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

    def join_doc(self, project_id: str, doc_id: str) -> DocJoinData:
        logger.info("Joining doc %s in project %s via realtime socket", doc_id, project_id)
        with LegacySocketConnection(self.config, self.session_manager, project_id) as connection:
            _ = connection.recv()
            _ = connection.recv()
            connection.send_event_with_ack(
                ack_id=1,
                event_name="joinDoc",
                args=[doc_id, {"encodeRanges": True, "supportsHistoryOT": True}],
            )

            for _ in range(20):
                message = connection.recv()
                if not message.startswith("6:::1+"):
                    continue

                payload = json.loads(message.split("+", 1)[1])
                if len(payload) < 6:
                    raise RuntimeError("joinDoc returned unexpected structure")

                logger.debug("Received joinDoc ack for doc %s, version=%s", doc_id, payload[2])
                return DocJoinData(
                    snapshot_lines=payload[1],
                    version=payload[2],
                    ranges=payload[3],
                    comments=payload[4],
                    ot_type=payload[5],
                )

        raise RuntimeError("Failed to receive joinDoc ack response")

    def apply_text_operation(
        self,
        project_id: str,
        doc_id: str,
        version: int,
        operations: list[dict[str, Any]],
    ) -> None:
        logger.info("Applying OT operation to doc %s (project %s, version %s)", doc_id, project_id, version)
        with LegacySocketConnection(self.config, self.session_manager, project_id) as connection:
            _ = connection.recv()
            _ = connection.recv()
            connection.send_event_with_ack(
                ack_id=1,
                event_name="joinDoc",
                args=[doc_id, {"encodeRanges": True, "supportsHistoryOT": True}],
            )
            _ = connection.recv()

            connection.send_event_with_ack(
                ack_id=2,
                event_name="applyOtUpdate",
                args=[
                    doc_id,
                    {
                        "doc": doc_id,
                        "op": operations,
                        "v": version,
                    },
                ],
            )

            for _ in range(20):
                message = connection.recv()
                if message.startswith("5:::"):
                    payload = json.loads(message[4:])
                    event_name = payload.get("name")
                    args = payload.get("args", [])
                    if event_name == "otUpdateApplied" and args:
                        update = args[0]
                        if update.get("doc") == doc_id:
                            logger.debug("OT update applied to doc %s", doc_id)
                            return
                    if event_name == "otUpdateError":
                        raise RuntimeError(f"applyOtUpdate failed: {payload}")
                if message.startswith("6:::2+"):
                    payload = json.loads(message.split("+", 1)[1])
                    if payload and payload[0] is not None:
                        raise RuntimeError(f"applyOtUpdate returned error: {payload[0]}")
                if "otUpdateError" in message:
                    raise RuntimeError(f"applyOtUpdate failed: {message}")

        raise RuntimeError("Failed to receive applyOtUpdate confirmation")
