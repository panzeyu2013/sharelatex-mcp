from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from requests.structures import CaseInsensitiveDict

import sharelatex_mcp.config as config_module
import sharelatex_mcp.realtime as realtime_module
from sharelatex_mcp.diff_engine import compute_diff_operations
from sharelatex_mcp.doc_editor import DocEditor
from sharelatex_mcp.errors import OTConflictError
from sharelatex_mcp.http import BinaryHttpResult, HttpResult
from sharelatex_mcp.projects import ProjectClient, ProjectEntity
from sharelatex_mcp.session import OverleafSessionManager
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
    compile_payload = {
        "pdfDownloadDomain": "https://clsi.example",
        "clsiServerId": "server-1",
    }
    client._remember_compile_download_origins(compile_payload)

    resolved = client._resolve_output_file_url(
        {"url": "/build/abc/output.log"},
        compile_payload,
    )

    assert resolved == "https://clsi.example/build/abc/output.log?clsiserverid=server-1"


def test_resolve_output_file_url_rejects_untrusted_origin() -> None:
    client = _make_project_client()

    with pytest.raises(RuntimeError, match="not trusted"):
        client._resolve_output_file_url(
            {"url": "http://127.0.0.1:8080/admin"},
            {},
        )


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_session_does_not_treat_error_response_as_logged_in(status_code: int) -> None:
    manager = object.__new__(OverleafSessionManager)
    manager.http = SimpleNamespace(
        get=lambda _: HttpResult(
            status_code=status_code,
            headers=CaseInsensitiveDict(),
            text="",
            url="https://overleaf.example/project",
        )
    )

    assert manager.is_logged_in() is False


def test_session_treats_successful_project_page_as_logged_in() -> None:
    manager = object.__new__(OverleafSessionManager)
    manager.http = SimpleNamespace(
        get=lambda _: HttpResult(
            status_code=200,
            headers=CaseInsensitiveDict(),
            text="",
            url="https://overleaf.example/project",
        )
    )

    assert manager.is_logged_in() is True


def test_session_network_runtime_error_is_not_treated_as_logged_in() -> None:
    manager = object.__new__(OverleafSessionManager)
    manager.http = SimpleNamespace(
        get=lambda _: (_ for _ in ()).throw(RuntimeError("offline"))
    )

    assert manager.is_logged_in() is False


def test_config_file_is_created_with_owner_only_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config" / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_file.parent)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)

    with pytest.raises(SystemExit):
        config_module._ensure_config_file()

    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_load_config_repairs_existing_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "base_url": "https://overleaf.example",
                "email": "user@example.com",
                "password": "secret",
            }
        ),
        encoding="utf-8",
    )
    config_file.chmod(0o644)
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)

    config_module.load_config()

    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_load_config_accepts_symlinked_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-config.json"
    target.write_text(
        json.dumps(
            {
                "base_url": "https://overleaf.example",
                "email": "user@example.com",
                "password": "secret",
            }
        ),
        encoding="utf-8",
    )
    config_file = tmp_path / "config.json"
    config_file.symlink_to(target)
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)

    result = config_module.load_config()

    assert result.base_url == "https://overleaf.example"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_download_file_accepts_filename_in_current_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary_result = BinaryHttpResult(
        status_code=200,
        headers=CaseInsensitiveDict(),
        content=b"content",
        url="https://overleaf.example/project/doc/download",
    )
    session_manager = SimpleNamespace(
        config=SimpleNamespace(base_url="https://overleaf.example"),
        http=SimpleNamespace(get_bytes=lambda _: binary_result),
    )
    client = ProjectClient(session_manager)
    monkeypatch.setattr(
        client,
        "_resolve_entity_by_path",
        lambda _project_id, _path: ProjectEntity(
            path="/main.tex",
            type="doc",
            entity_id="aaaaaaaaaaaaaaaaaaaaaaaa",
        ),
    )
    monkeypatch.chdir(tmp_path)

    result = client.download_file(
        "0123456789abcdef01234567",
        "/main.tex",
        "result.tex",
    )

    expected_path = tmp_path / "result.tex"
    assert result["output_path"] == os.fspath(expected_path)
    assert expected_path.read_bytes() == b"content"


def test_download_pdf_accepts_filename_in_current_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary_result = BinaryHttpResult(
        status_code=200,
        headers=CaseInsensitiveDict({"Content-Type": "application/pdf"}),
        content=b"%PDF-1.7",
        url="https://overleaf.example/build/output.pdf",
    )
    session_manager = SimpleNamespace(
        config=SimpleNamespace(base_url="https://overleaf.example"),
        http=SimpleNamespace(get_bytes_absolute=lambda _: binary_result),
    )
    client = ProjectClient(session_manager)
    monkeypatch.chdir(tmp_path)
    compile_result = {
        "status": "success",
        "outputFiles": [
            {
                "path": "output.pdf",
                "url": "/build/output.pdf",
            }
        ],
    }
    client._store_compile_result(
        "0123456789abcdef01234567",
        compile_result,
    )

    result = client.download_pdf(
        "0123456789abcdef01234567",
        compile_result=compile_result,
        output_path="result.pdf",
    )

    expected_path = tmp_path / "result.pdf"
    assert result["output_path"] == os.fspath(expected_path)
    assert expected_path.read_bytes() == b"%PDF-1.7"


def test_default_download_path_rejects_project_path_traversal() -> None:
    client = _make_project_client()

    with pytest.raises(RuntimeError, match="escapes"):
        client._default_download_output_path(
            "0123456789abcdef01234567",
            "/../../outside",
        )


def test_compile_result_rejects_forged_same_origin_url() -> None:
    seen: list[str] = []
    http = SimpleNamespace(
        get_absolute=lambda url: (
            seen.append(url)
            or HttpResult(
                status_code=200,
                headers=CaseInsensitiveDict(),
                text="real compile log",
                url=url,
            )
        )
    )
    client = ProjectClient(
        SimpleNamespace(
            config=SimpleNamespace(base_url="https://overleaf.example"),
            http=http,
        )
    )
    project_id = "0123456789abcdef01234567"
    compile_result = {
        "status": "success",
        "outputFiles": [
            {
                "path": "output.log",
                "url": "/build/real/output.log",
            }
        ],
    }
    client._store_compile_result(project_id, compile_result)
    forged = {
        **compile_result,
        "outputFiles": [
            {
                "path": "output.log",
                "url": "/admin/user/export",
            }
        ],
    }

    result = client.get_compile_logs(project_id, forged)

    assert seen == ["https://overleaf.example/build/real/output.log"]
    assert result["output_log"] == "real compile log"


def test_compile_result_without_server_token_is_rejected() -> None:
    client = _make_project_client()

    with pytest.raises(RuntimeError, match="must come from compile_project"):
        client.get_compile_logs(
            "0123456789abcdef01234567",
            {
                "status": "success",
                "outputFiles": [
                    {
                        "path": "output.log",
                        "url": "/admin/user/export",
                    }
                ],
            },
        )


def test_list_files_with_ids_uses_ttl_cache_then_force_refresh() -> None:
    project_id = "0123456789abcdef01234567"

    def make_tree(name: str, entity_id: str) -> dict:
        return {
            "rootFolder": [
                {
                    "name": "rootFolder",
                    "_id": "f" * 24,
                    "docs": [{"name": name, "_id": entity_id}],
                    "fileRefs": [],
                    "folders": [],
                }
            ]
        }

    state = {
        "tree": make_tree("old.tex", "a" * 24),
        "calls": 0,
    }

    class FakeRealtime:
        def join_project(self, _project_id):
            state["calls"] += 1
            return realtime_module.ProjectJoinData(
                project=state["tree"],
                permissions_level=None,
                protocol_version=None,
                public_id=None,
            )

    client = _make_project_client()
    client.realtime_client = FakeRealtime()

    first = client.list_files_with_ids(project_id)
    state["tree"] = make_tree("new.tex", "b" * 24)
    second = client.list_files_with_ids(project_id)
    assert [entity.path for entity in first] == ["/old.tex"]
    assert [entity.path for entity in second] == ["/old.tex"]
    assert state["calls"] == 1

    third = client.list_files_with_ids(project_id, force_refresh=True)
    assert [entity.path for entity in third] == ["/new.tex"]
    assert state["calls"] == 2


def test_resolve_entity_by_path_uses_fresh_cache_but_refreshes_on_miss() -> None:
    project_id = "0123456789abcdef01234567"

    def make_tree(name: str, entity_id: str) -> dict:
        return {
            "rootFolder": [
                {
                    "name": "rootFolder",
                    "_id": "f" * 24,
                    "docs": [{"name": name, "_id": entity_id}],
                    "fileRefs": [],
                    "folders": [],
                }
            ]
        }

    state = {
        "tree": make_tree("a.tex", "a" * 24),
        "calls": 0,
    }

    class FakeRealtime:
        def join_project(self, _project_id):
            state["calls"] += 1
            return realtime_module.ProjectJoinData(
                project=state["tree"],
                permissions_level=None,
                protocol_version=None,
                public_id=None,
            )

    client = _make_project_client()
    client.realtime_client = FakeRealtime()

    first = client._resolve_entity_by_path(project_id, "/a.tex")
    state["tree"] = make_tree("b.tex", "b" * 24)
    second = client._resolve_entity_by_path(project_id, "/a.tex")
    assert first.entity_id == second.entity_id == "a" * 24
    assert state["calls"] == 1

    third = client._resolve_entity_by_path(project_id, "/b.tex")
    assert third.entity_id == "b" * 24
    assert state["calls"] == 2


def test_csrf_request_reauthenticates_after_login_redirect() -> None:
    force_refresh_calls: list[bool] = []
    invalidations: list[bool] = []
    session_manager = SimpleNamespace(
        config=SimpleNamespace(base_url="https://overleaf.example"),
        get_csrf_token=lambda project_id, force_refresh: (
            force_refresh_calls.append(force_refresh) or "token"
        ),
        invalidate_login=lambda: invalidations.append(True),
    )
    client = ProjectClient(session_manager)
    responses = iter(
        [
            HttpResult(
                status_code=302,
                headers=CaseInsensitiveDict({"Location": "/login"}),
                text="",
                url="https://overleaf.example/login",
            ),
            HttpResult(
                status_code=204,
                headers=CaseInsensitiveDict(),
                text="",
                url="https://overleaf.example/project",
            ),
        ]
    )

    result = client._request_with_csrf_retry(
        "0123456789abcdef01234567",
        lambda _headers: next(responses),
    )

    assert result.status_code == 204
    assert force_refresh_calls == [False, True]
    assert invalidations == [True]


def test_delete_entity_rejects_non_2xx_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_project_client()
    monkeypatch.setattr(
        client,
        "_delete_with_csrf",
        lambda **_kwargs: HttpResult(
            status_code=302,
            headers=CaseInsensitiveDict({"Location": "/login"}),
            text="",
            url="https://overleaf.example/login",
        ),
    )

    with pytest.raises(RuntimeError, match="status code: 302"):
        client.delete_entity(
            "0123456789abcdef01234567",
            "doc",
            "a" * 24,
        )


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"retry_on_500": 6}, "retry_on_500"),
        ({"retry_delay_seconds": -1}, "retry_delay_seconds"),
        ({"min_interval_seconds": float("inf")}, "min_interval_seconds"),
        ({"check": ""}, "check"),
    ],
)
def test_compile_project_rejects_unbounded_control_parameters(
    kwargs: dict,
    error: str,
) -> None:
    client = _make_project_client()

    with pytest.raises(RuntimeError, match=error):
        client.compile_project(
            "0123456789abcdef01234567",
            root_doc_id="a" * 24,
            **kwargs,
        )


def test_edit_lost_ack_recovery_compares_exact_submitted_content() -> None:
    class FakeRealtime:
        def join_doc_write(self, project_id, doc_id, diff_fn) -> None:
            diff_fn("hello old world")
            raise OTConflictError("ack lost")

        def join_doc_read(self, project_id, doc_id) -> str:
            return "hello new world"

    realtime = FakeRealtime()
    client = SimpleNamespace(
        realtime_client=realtime,
        _resolve_entity_by_path=lambda _project_id, _path: ProjectEntity(
            path="/main.tex",
            type="doc",
            entity_id="aaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        _invalidate_caches=lambda _project_id: None,
    )

    result = DocEditor(client).edit(
        "0123456789abcdef01234567",
        "/main.tex",
        [{"old": "old", "new": "new"}],
    )

    assert result["changed"] is True
    assert result["message"] == "Edits were already applied (ack recovery)"


def test_edit_lost_ack_recovery_rejects_unrelated_content() -> None:
    class FakeRealtime:
        def join_doc_write(self, project_id, doc_id, diff_fn) -> None:
            diff_fn("hello old world")
            raise OTConflictError("ack lost")

        def join_doc_read(self, project_id, doc_id) -> str:
            return "concurrent unrelated edit"

    realtime = FakeRealtime()
    client = SimpleNamespace(
        realtime_client=realtime,
        _resolve_entity_by_path=lambda _project_id, _path: ProjectEntity(
            path="/main.tex",
            type="doc",
            entity_id="aaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        _invalidate_caches=lambda _project_id: None,
    )

    with pytest.raises(OTConflictError, match="ack lost"):
        DocEditor(client).edit(
            "0123456789abcdef01234567",
            "/main.tex",
            [{"old": "old", "new": "new"}],
        )


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
