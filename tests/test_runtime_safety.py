from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from requests.structures import CaseInsensitiveDict

import sharelatex_mcp.realtime as realtime_module
from sharelatex_mcp.http import HttpResult
from sharelatex_mcp.projects import ProjectClient, ProjectEntity
from sharelatex_mcp.validation import validate_http_path, validate_project_id


def _make_project_client() -> ProjectClient:
    session_manager = SimpleNamespace(config=SimpleNamespace(base_url="https://overleaf.example"))
    return ProjectClient(session_manager)


def test_validate_http_path_rejects_dot_segments_and_absolute_urls() -> None:
    with pytest.raises(RuntimeError):
        validate_http_path("/project/../user/settings")
    with pytest.raises(RuntimeError):
        validate_http_path("https://other.example/project")


def test_validate_project_id_requires_object_id() -> None:
    assert validate_project_id("0123456789abcdef01234567") == "0123456789abcdef01234567"
    with pytest.raises(RuntimeError):
        validate_project_id("../user/settings")


def test_resolve_output_file_url_uses_pdf_download_domain_and_clsi_server_id() -> None:
    client = _make_project_client()

    resolved = client._resolve_output_file_url(
        {"url": "/build/abc/output.log"},
        {
            "pdfDownloadDomain": "https://clsi.example",
            "clsiServerId": "server-1",
        },
    )

    assert resolved == "https://clsi.example/build/abc/output.log?clsiserverid=server-1"


def test_compile_cache_key_includes_root_doc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_project_client()
    project_id = "0123456789abcdef01234567"
    root_a = "aaaaaaaaaaaaaaaaaaaaaaaa"
    root_b = "bbbbbbbbbbbbbbbbbbbbbbbb"
    client._compile_cache[project_id] = (
        time.time(),
        {"status": "too-recently-compiled", "rootDoc_id": root_a},
        (root_a, False, "silent", False, False),
    )

    def fake_post_json_with_csrf(**kwargs):
        return HttpResult(
            status_code=200,
            headers=CaseInsensitiveDict(),
            text=json.dumps({"status": "success", "outputFiles": []}),
            url="https://overleaf.example/project/compile",
        )

    monkeypatch.setattr(client, "_post_json_with_csrf", fake_post_json_with_csrf)

    result = client.compile_project(project_id, root_doc_id=root_b)

    assert result["cached"] is False
    assert result["rootDoc_id"] == root_b


def test_realtime_apply_ot_success_ack_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        "1::",
        "1::",
        "6:::1+" + json.dumps([None, ["old"], 7, [], {}, "sharejs-text-ot"]),
        "6:::2+[]",
    ]

    class FakeConnection:
        def __init__(self, *args, **kwargs) -> None:
            self.sent = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def recv(self) -> str:
            return messages.pop(0)

        def send_event_with_ack(self, ack_id: int, event_name: str, args: list) -> None:
            self.sent.append((ack_id, event_name, args))

        def drain_initial_messages(self, expected_count: int = 2) -> None:
            for _ in range(expected_count):
                self.recv()

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(),
        SimpleNamespace(),
    )

    result = client.join_doc_and_apply_ot(
        "0123456789abcdef01234567",
        "aaaaaaaaaaaaaaaaaaaaaaaa",
        [{"p": 0, "i": "new"}],
    )

    assert result.version == 7


def test_write_file_uses_diff_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    """write_file must send diff-based OT ops, not full-replacement ops."""
    client = _make_project_client()

    captured_ops: list[list[dict[str, Any]]] = []

    def fake_join_doc_and_apply_ot(
        self: Any, project_id: str, doc_id: str, operations: list[dict[str, Any]]
    ) -> Any:
        captured_ops.append(operations)
        return SimpleNamespace(version=1)

    monkeypatch.setattr(
        "sharelatex_mcp.realtime.RealtimeProjectClient.join_doc_and_apply_ot",
        fake_join_doc_and_apply_ot,
    )

    def fake_resolve_entity_by_path(
        project_id: str, path: str
    ) -> ProjectEntity:
        return ProjectEntity(path=path, type="doc", entity_id="doc123")

    monkeypatch.setattr(client, "_resolve_entity_by_path", fake_resolve_entity_by_path)

    def fake_read_file(
        project_id: str, path: str
    ) -> dict[str, str]:
        return {"content": "hello world"}

    monkeypatch.setattr(client, "read_file", fake_read_file)

    result = client.write_file("0123456789abcdef01234567", "main.tex", "hello there world")

    assert result["changed"] is True
    assert captured_ops
    # Verify diff-based ops are used (not full replacement)
    assert captured_ops[0] == [{"p": 6, "i": "there "}]


def test_write_file_falls_back_on_diff_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """write_file must fall back to full replacement when diff computation fails."""
    client = _make_project_client()

    captured_ops: list[list[dict[str, Any]]] = []

    def fake_join_doc_and_apply_ot(
        self: Any, project_id: str, doc_id: str, operations: list[dict[str, Any]]
    ) -> Any:
        captured_ops.append(operations)
        return SimpleNamespace(version=1)

    monkeypatch.setattr(
        "sharelatex_mcp.realtime.RealtimeProjectClient.join_doc_and_apply_ot",
        fake_join_doc_and_apply_ot,
    )

    def fake_resolve_entity_by_path(
        project_id: str, path: str
    ) -> ProjectEntity:
        return ProjectEntity(path=path, type="doc", entity_id="doc123")

    monkeypatch.setattr(client, "_resolve_entity_by_path", fake_resolve_entity_by_path)

    def fake_read_file(
        project_id: str, path: str
    ) -> dict[str, str]:
        return {"content": "hello world"}

    monkeypatch.setattr(client, "read_file", fake_read_file)

    # Simulate diff computation failure
    def fake_diff_failure(old: str, new: str) -> list[dict[str, Any]]:
        raise MemoryError("simulated diff failure")

    monkeypatch.setattr(
        "sharelatex_mcp.projects._compute_diff_operations",
        fake_diff_failure,
    )

    result = client.write_file("0123456789abcdef01234567", "main.tex", "hello there world")

    assert result["changed"] is True
    assert captured_ops
    assert captured_ops[0] == [
        {"p": 0, "d": "hello world"},
        {"p": 0, "i": "hello there world"},
    ]
