"""Tests for the MCP tool layer: async write/edit routing, job tools, validation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from sharelatex_mcp.config import AppConfig
from sharelatex_mcp.diff_engine import MAX_FILE_SIZE
from sharelatex_mcp.jobs import JobStore
from sharelatex_mcp.server import create_server


def _fake_config(threshold: int = 262144) -> AppConfig:
    return AppConfig(
        base_url="https://overleaf.example",
        email="user@example.com",
        password="secret",
        timeout_seconds=60,
        allow_insecure_http=False,
        project_id=None,
        log_level="INFO",
        async_write_threshold_bytes=threshold,
    )


def _fake_project_client() -> SimpleNamespace:
    # Constructible into DocEditor; any actual doc_editor.write/edit call will
    # fail with AttributeError, but the async path never calls it synchronously.
    return SimpleNamespace(realtime_client=SimpleNamespace())


def _normalize(result) -> dict:
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _make_server(job_store: JobStore | None = None, threshold: int = 262144):
    return create_server(
        config=_fake_config(threshold=threshold),
        project_client=_fake_project_client(),
        job_store=job_store if job_store is not None else JobStore(max_workers=1),
    )


async def test_write_async_mode_returns_job_id() -> None:
    server = _make_server()
    result = await server.call_tool(
        "write",
        {"project_id": "0" * 24, "path": "/x.tex", "content": "hello", "async_mode": True},
    )
    payload = _normalize(result)
    assert payload["async"] is True
    assert payload["status"] == "queued"
    assert payload["job_id"]
    assert payload["project_id"] == "0" * 24


async def test_write_auto_async_over_threshold() -> None:
    server = _make_server(threshold=10)
    result = await server.call_tool(
        "write",
        {"project_id": "0" * 24, "path": "/x.tex", "content": "x" * 100},
    )
    payload = _normalize(result)
    assert payload["async"] is True
    assert payload["job_id"]


async def test_write_oversized_content_rejected_before_async() -> None:
    server = _make_server(threshold=10)
    with pytest.raises(ToolError, match="MB limit"):
        await server.call_tool(
            "write",
            {
                "project_id": "0" * 24,
                "path": "/x.tex",
                "content": "x" * (MAX_FILE_SIZE + 1),
                "async_mode": True,
            },
        )


async def test_edit_async_mode_returns_job_id() -> None:
    server = _make_server()
    result = await server.call_tool(
        "edit",
        {
            "project_id": "0" * 24,
            "path": "/x.tex",
            "edits": [{"old": "a", "new": "b"}],
            "async_mode": True,
        },
    )
    payload = _normalize(result)
    assert payload["async"] is True
    assert payload["job_id"]


async def test_edit_invalid_edits_rejected_before_async() -> None:
    server = _make_server()
    with pytest.raises(ToolError, match="missing 'old' or 'new'"):
        await server.call_tool(
            "edit",
            {
                "project_id": "0" * 24,
                "path": "/x.tex",
                "edits": [{"only": "x"}],
                "async_mode": True,
            },
        )


async def test_get_job_status_unknown_job() -> None:
    server = _make_server()
    result = await server.call_tool("get_job_status", {"job_id": "nope"})
    payload = _normalize(result)
    assert payload["status"] == "not-found"


async def test_wait_job_rejects_invalid_timeout() -> None:
    server = _make_server()
    for bad in (-1, 301):
        with pytest.raises(ToolError, match="timeout_seconds"):
            await server.call_tool(
                "wait_job", {"job_id": "x", "timeout_seconds": bad}
            )


async def test_wait_job_unknown_job_returns_immediately() -> None:
    server = _make_server()
    result = await server.call_tool("wait_job", {"job_id": "nope", "timeout_seconds": 0})
    payload = _normalize(result)
    assert payload["status"] == "not-found"
