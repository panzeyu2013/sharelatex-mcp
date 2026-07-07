from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from requests.structures import CaseInsensitiveDict

import sharelatex_mcp.realtime as realtime_module
from sharelatex_mcp.diff_engine import compute_diff_operations
from sharelatex_mcp.http import HttpResult
from sharelatex_mcp.projects import ProjectClient
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


def test_join_doc_write_applies_ot(monkeypatch: pytest.MonkeyPatch) -> None:
    """join_doc_write must correctly drain, joinDoc, diff, and apply OT."""
    messages = [
        "1::",
        "1::",
        "6:::1+" + json.dumps([None, ["hello world"], 7, [], {}, "sharejs-text-ot"]),
        "6:::2+[]",
    ]

    class FakeConnection:
        def __init__(self, *args, **kwargs) -> None:
            self.sent = []
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda _: None  # no-op for test

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

        def _send_locked(self, data: str) -> None:
            pass  # no-op for test

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=30),
        SimpleNamespace(),
    )

    captured_content: list[str] = []

    def diff_fn(content: str) -> list[dict[str, str | int]]:
        captured_content.append(content)
        return [{"p": 6, "i": "there "}]

    client.join_doc_write(
        "0123456789abcdef01234567",
        "aaaaaaaaaaaaaaaaaaaaaaaa",
        diff_fn,
    )

    assert captured_content == ["hello world"]


def test_write_uses_diff_operations() -> None:
    """compute_diff_operations must produce minimal diff ops, not full replacement."""
    result = compute_diff_operations("hello world", "hello there world")
    # Verify diff-based ops are used (not full replacement)
    assert result == [{"p": 6, "i": "there "}]


def test_write_falls_back_to_full_replace_on_large_diff() -> None:
    """compute_diff_operations must fall back to full replacement for near-total changes."""
    # Two 50KB strings that differ in every position → pre-scan should trigger full-replace
    old = "A" * 50000
    new = "B" * 50000
    result = compute_diff_operations(old, new)
    # Full replacement: delete all of old, insert all of new
    assert result == [{"p": 0, "d": old}, {"p": 0, "i": new}]
