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
from sharelatex_mcp.diff_engine import MAX_FILE_SIZE, compute_diff_operations
from sharelatex_mcp.doc_editor import DocEditor
from sharelatex_mcp.errors import (
    CacheConsistencyError,
    FileSizeError,
    FileTypeError,
    OTConflictError,
    OTTransportError,
    ParamValidationError,
)
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
        def join_doc_write(self, project_id, doc_id, diff_fn, progress=None) -> None:
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
        def join_doc_write(self, project_id, doc_id, diff_fn, progress=None) -> None:
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
    """join_doc_write must correctly drain, joinProject, joinDoc, diff, and apply OT."""
    messages = [
        "1::",
        '5:::{"name":"joinProjectResponse","args":[null]}',
        "6:::2+" + json.dumps([None, ["hello world"], 7, [], {}, "sharejs-text-ot"]),
        "6:::3",
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

        def drain_initial_messages(self, expected_count: int = 1) -> None:
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


def test_read_reports_trailing_newline_line_count() -> None:
    """read must split on \\n only so a trailing newline keeps its final empty line."""
    realtime = SimpleNamespace(join_doc_read=lambda _project_id, _doc_id: "line1\nline2\n")
    client = SimpleNamespace(
        realtime_client=realtime,
        _resolve_entity_by_path=lambda _project_id, _path: ProjectEntity(
            path="/main.tex", type="doc", entity_id="a" * 24,
        ),
        _invalidate_caches=lambda _project_id: None,
    )

    result = DocEditor(client).read("0" * 24, "/main.tex")

    assert result["total_lines"] == 3
    assert result["returned_lines"] == 3
    assert result["content"] == "1: line1\n2: line2\n3: "


def test_read_splits_on_newline_only_not_other_separators() -> None:
    """splitlines() splits on \\r/\\x0b/\\u2028 etc.; read must not, or line numbers diverge."""
    realtime = SimpleNamespace(
        join_doc_read=lambda _project_id, _doc_id: "a\x0bb\nc",
    )
    client = SimpleNamespace(
        realtime_client=realtime,
        _resolve_entity_by_path=lambda _project_id, _path: ProjectEntity(
            path="/main.tex", type="doc", entity_id="a" * 24,
        ),
        _invalidate_caches=lambda _project_id: None,
    )

    result = DocEditor(client).read("0" * 24, "/main.tex")

    assert result["total_lines"] == 2
    assert result["content"] == "1: a\x0bb\n2: c"


def test_write_create_normalizes_parent_path_before_resolve() -> None:
    """write to 'chapters/intro.tex' must resolve the '/chapters' folder."""
    resolved_folders: list[str] = []

    class FakeRealtime:
        def join_doc_write(self, _project_id, _doc_id, _diff_fn, progress=None) -> None:
            return None

    client = SimpleNamespace(
        realtime_client=FakeRealtime(),
        _resolve_entity_by_path=lambda _project_id, _path: (_ for _ in ()).throw(
            RuntimeError("not found")
        ),
        _resolve_folder_id_by_path=lambda _project_id, folder_path: (
            resolved_folders.append(folder_path) or ("f" * 24, folder_path)
        ),
        _post_json_with_csrf=lambda **_kwargs: HttpResult(
            status_code=200,
            headers=CaseInsensitiveDict(),
            text=json.dumps({"_id": "a" * 24}),
            url="https://overleaf.example/project/doc",
        ),
        _delete_with_csrf=lambda **_kwargs: HttpResult(
            status_code=200,
            headers=CaseInsensitiveDict(),
            text="",
            url="https://overleaf.example/project/doc",
        ),
        _cache_upsert=lambda *_args, **_kwargs: None,
        _invalidate_caches=lambda _project_id: None,
    )

    result = DocEditor(client).write("0" * 24, "chapters/intro.tex", "hello")

    assert resolved_folders == ["/chapters"]
    assert result["created"] is True
    assert result["path"] == "/chapters/intro.tex"


def test_edit_applied_count_excludes_identity_edits() -> None:
    """edits_applied must count only edits that actually change the document."""

    class FakeRealtime:
        def join_doc_write(self, _project_id, _doc_id, diff_fn, progress=None) -> None:
            diff_fn("hello world")

    client = SimpleNamespace(
        realtime_client=FakeRealtime(),
        _resolve_entity_by_path=lambda _project_id, _path: ProjectEntity(
            path="/main.tex", type="doc", entity_id="a" * 24,
        ),
        _invalidate_caches=lambda _project_id: None,
    )

    result = DocEditor(client).edit(
        "0" * 24,
        "/main.tex",
        [{"old": "hello", "new": "hello"}, {"old": "world", "new": "planet"}],
    )

    assert result["changed"] is True
    assert result["edits_applied"] == 1


@pytest.mark.timeout(5)
def test_wait_for_ack_fails_fast_when_connection_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped connection must fail fast instead of spinning until the ack deadline."""

    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda _t: None
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def _send_locked(self, _data: str) -> None:
            return None

        def drain_initial_messages(self, _expected_count: int = 1) -> None:
            for _ in range(_expected_count):
                self.recv()

        def send_event_with_ack(self, *_args, **_kwargs) -> None:
            return None

        def recv(self) -> str:
            self.calls += 1
            if self.calls <= 2:
                return "1::"
            if self.calls == 3:
                return '5:::{"name":"joinProjectResponse","args":[null]}'
            if self.calls == 4:
                return "6:::2+" + json.dumps(
                    [None, ["hello world"], 7, [], {}, "sharejs-text-ot"]
                )
            raise realtime_module.WebSocketError("connection closed")

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=60),
        SimpleNamespace(),
    )

    with pytest.raises(realtime_module.OTTransportError, match="connection lost"):
        client.join_doc_write(
            "0" * 24,
            "a" * 24,
            lambda _content: [{"p": 6, "i": "there "}],
        )


def test_wait_for_ack_uses_heartbeat_aware_recv_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recv waits must be at least the socket.io heartbeat interval so inline
    heartbeat replies keep the connection alive."""
    seen_timeouts: list[float] = []
    messages = [
        "1::",
        '5:::{"name":"joinProjectResponse","args":[null]}',
        "6:::2+" + json.dumps([None, ["hello world"], 7, [], {}, "sharejs-text-ot"]),
        "6:::3",
    ]

    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda t: seen_timeouts.append(t)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def recv(self) -> str:
            return messages.pop(0)

        def _send_locked(self, _data: str) -> None:
            return None

        def drain_initial_messages(self, _expected_count: int = 1) -> None:
            for _ in range(_expected_count):
                self.recv()

        def send_event_with_ack(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=15),
        SimpleNamespace(),
    )

    client.join_doc_write(
        "0" * 24,
        "a" * 24,
        lambda _content: [{"p": 6, "i": "there "}],
    )

    assert any(t >= realtime_module._HEARTBEAT_INTERVAL_SECONDS for t in seen_timeouts)
    # The socket timeout must be reset to the configured value before sending
    # the OT payload, so a slow send is not throttled by the leftover
    # per-recv timeout from the joinDoc phase.
    assert 15 in seen_timeouts


class _LockStub:
    def __init__(self, records: list, project_id: str) -> None:
        self._records = records
        self._project_id = project_id

    def __enter__(self):
        self._records.append(("acquire", self._project_id))

    def __exit__(self, *_args):
        self._records.append(("release", self._project_id))
        return False


def test_per_project_lock_is_shared_and_reentrant() -> None:
    client = _make_project_client()

    lock_a = client._op_lock("0" * 24)
    assert lock_a is client._op_lock("0" * 24)
    assert client._op_lock("1" * 24) is not lock_a

    with lock_a, client._op_lock("0" * 24):  # reentrant
        pass


def test_per_project_lock_handles_all_keyword_calls() -> None:
    """The decorator must not crash when project_id is passed by keyword only."""
    import threading
    import types

    from sharelatex_mcp.projects import _per_project_lock

    records: list[str] = []

    class FakeClient:
        def __init__(self) -> None:
            self._locks: dict[str, threading.RLock] = {}

        def _op_lock(self, project_id: str) -> threading.RLock:
            return self._locks.setdefault(project_id, threading.RLock())

    @_per_project_lock
    def op(self, project_id: str, name: str) -> str:
        records.append(project_id)
        return name

    fake = FakeClient()
    bound = types.MethodType(op, fake)

    assert bound(project_id="0" * 24, name="x") == "x"
    assert bound(project_id="1" * 24, name="y") == "y"
    assert records == ["0" * 24, "1" * 24]


def test_doc_editor_read_acquires_per_project_lock() -> None:
    records: list = []
    realtime = SimpleNamespace(
        join_doc_read=lambda _project_id, _doc_id: "line1\nline2\n",
    )
    client = SimpleNamespace(
        realtime_client=realtime,
        _resolve_entity_by_path=lambda _project_id, _path: ProjectEntity(
            path="/main.tex", type="doc", entity_id="a" * 24,
        ),
        _invalidate_caches=lambda _project_id: None,
        _op_lock=lambda project_id: _LockStub(records, project_id),
    )

    DocEditor(client).read("0" * 24, "/main.tex")

    assert records == [("acquire", "0" * 24), ("release", "0" * 24)]


def test_join_doc_write_reports_progress_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        "1::",
        '5:::{"name":"joinProjectResponse","args":[null]}',
        "6:::2+" + json.dumps([None, ["hello"], 3, [], {}, "sharejs-text-ot"]),
        "6:::3",
    ]

    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda _t: None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def recv(self) -> str:
            return messages.pop(0)

        def _send_locked(self, _data: str) -> None:
            return None

        def drain_initial_messages(self, _expected_count: int = 1) -> None:
            for _ in range(_expected_count):
                self.recv()

        def send_event_with_ack(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=60),
        SimpleNamespace(),
    )

    events: list[tuple[int, int]] = []

    client.join_doc_write(
        "0" * 24,
        "a" * 24,
        lambda _content: [{"p": 6, "i": "there "}],
        progress=lambda done, total, _message: events.append((done, total)),
    )

    assert events == [(1, 4), (2, 4), (3, 4), (4, 4)]


@pytest.mark.timeout(5)
def test_ot_update_error_is_reported_as_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        "1::",
        '5:::{"name":"joinProjectResponse","args":[null]}',
        "6:::2+" + json.dumps([None, ["hello"], 3, [], {}, "sharejs-text-ot"]),
        "5:::" + json.dumps({"name": "otUpdateError", "args": [{"doc": "a" * 24}]}),
    ]

    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda _t: None
            self._messages = list(messages)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def recv(self) -> str:
            return self._messages.pop(0)

        def _send_locked(self, _data: str) -> None:
            return None

        def drain_initial_messages(self, _expected_count: int = 1) -> None:
            for _ in range(_expected_count):
                self.recv()

        def send_event_with_ack(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=60),
        SimpleNamespace(),
    )

    with pytest.raises(OTConflictError, match="applyOtUpdate error"):
        client.join_doc_write(
            "0" * 24,
            "a" * 24,
            lambda _content: [{"p": 6, "i": "there "}],
        )


@pytest.mark.timeout(5)
def test_join_doc_write_bounds_total_time_by_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda _t: None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def recv(self) -> str:
            raise realtime_module.WebSocketTimeoutError("silent connection")

        def _send_locked(self, _data: str) -> None:
            return None

        def drain_initial_messages(self, _expected_count: int = 2) -> None:
            return None

        def send_event_with_ack(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=60),
        SimpleNamespace(),
    )

    started = time.time()
    # A silent connection fails first in the joinProject step → transport error;
    # it must surface as OTTransportError (not masked into OTConflictError) while
    # the total wall-clock stays bounded by the 0.3s budget.
    with pytest.raises(realtime_module.OTTransportError, match="joinProjectResponse"):
        client.join_doc_write(
            "0" * 24,
            "a" * 24,
            lambda _content: [{"p": 0, "i": "x"}],
            timeout=0.3,
        )
    assert time.time() - started < 5.0


# ---------------------------------------------------------------------------
# Additional review-driven tests: read edge cases, write recovery, rollback,
# retry paths, validation, progress, lock mutual exclusion
# ---------------------------------------------------------------------------


def test_read_handles_crlf_and_empty_and_slicing() -> None:
    def make_client(join_doc_read):
        realtime = SimpleNamespace(join_doc_read=join_doc_read)
        return SimpleNamespace(
            realtime_client=realtime,
            _resolve_entity_by_path=lambda _p, _path: ProjectEntity(
                path="/main.tex", type="doc", entity_id="a" * 24,
            ),
            _invalidate_caches=lambda _p: None,
        )

    # CRLF content is normalized per line; a trailing newline keeps its empty line
    crlf = DocEditor(make_client(lambda _p, _d: "a\r\nb\r\n")).read("0" * 24, "/main.tex")
    assert crlf["total_lines"] == 3
    assert crlf["content"] == "1: a\n2: b\n3: "

    # empty doc -> no lines, no phantom "1: "
    empty = DocEditor(make_client(lambda _p, _d: "")).read("0" * 24, "/main.tex")
    assert empty["total_lines"] == 0
    assert empty["content"] == ""

    # offset beyond the end -> empty result
    sliced = DocEditor(make_client(lambda _p, _d: "a\nb")).read(
        "0" * 24, "/main.tex", offset=5
    )
    assert sliced["returned_lines"] == 0
    assert sliced["content"] == ""

    # offset/limit slicing
    sliced = DocEditor(make_client(lambda _p, _d: "a\nb\nc\nd")).read(
        "0" * 24, "/main.tex", offset=1, limit=2
    )
    assert sliced["content"] == "2: b\n3: c"


def test_read_rejects_oversized_full_read_and_non_doc_entity() -> None:
    def make_client(join_doc_read, entity):
        realtime = SimpleNamespace(join_doc_read=join_doc_read)
        return SimpleNamespace(
            realtime_client=realtime,
            _resolve_entity_by_path=lambda _p, _path: entity,
            _invalidate_caches=lambda _p: None,
        )

    big = "x" * (MAX_FILE_SIZE + 1)
    with pytest.raises(FileSizeError):
        big_entity = ProjectEntity(path="/b.tex", type="doc", entity_id="a" * 24)
        DocEditor(make_client(lambda _p, _d: big, big_entity)).read("0" * 24, "/b.tex")

    with pytest.raises(FileTypeError):
        img_entity = ProjectEntity(path="/img.png", type="fileRef", entity_id="a" * 24)
        DocEditor(make_client(lambda _p, _d: "x", img_entity)).read("0" * 24, "/img.png")


def test_write_reports_unchanged_when_content_matches() -> None:
    class FakeRealtime:
        def join_doc_write(self, _project_id, _doc_id, diff_fn, progress=None) -> None:
            assert diff_fn("same content") == []

    client = SimpleNamespace(
        realtime_client=FakeRealtime(),
        _resolve_entity_by_path=lambda _p, _path: ProjectEntity(
            path="/main.tex", type="doc", entity_id="a" * 24,
        ),
        _invalidate_caches=lambda _p: None,
    )

    result = DocEditor(client).write("0" * 24, "/main.tex", "same content")

    assert result["changed"] is False
    assert "unchanged" in result["message"]


def test_write_lost_ack_recovery_on_transport_error() -> None:
    class FakeRealtime:
        def join_doc_write(self, _project_id, _doc_id, _diff_fn, progress=None) -> None:
            raise OTTransportError("ack lost")

        def join_doc_read(self, _project_id, _doc_id) -> str:
            return "the content"

    client = SimpleNamespace(
        realtime_client=FakeRealtime(),
        _resolve_entity_by_path=lambda _p, _path: ProjectEntity(
            path="/main.tex", type="doc", entity_id="a" * 24,
        ),
        _invalidate_caches=lambda _p: None,
    )

    result = DocEditor(client).write("0" * 24, "/main.tex", "the content")

    assert result["changed"] is True
    assert result["message"] == "Write already applied (ack recovery)"


def test_write_lost_ack_recovery_rejects_unrelated_content() -> None:
    class FakeRealtime:
        def join_doc_write(self, _project_id, _doc_id, _diff_fn, progress=None) -> None:
            raise OTTransportError("ack lost")

        def join_doc_read(self, _project_id, _doc_id) -> str:
            return "concurrent unrelated edit"

    client = SimpleNamespace(
        realtime_client=FakeRealtime(),
        _resolve_entity_by_path=lambda _p, _path: ProjectEntity(
            path="/main.tex", type="doc", entity_id="a" * 24,
        ),
        _invalidate_caches=lambda _p: None,
    )

    with pytest.raises(OTTransportError, match="ack lost"):
        DocEditor(client).write("0" * 24, "/main.tex", "the content")


def test_edit_lost_ack_recovery_on_transport_error() -> None:
    class FakeRealtime:
        def join_doc_write(self, _project_id, _doc_id, diff_fn, progress=None) -> None:
            diff_fn("hello old world")
            raise OTTransportError("ack lost")

        def join_doc_read(self, _project_id, _doc_id) -> str:
            return "hello new world"

    client = SimpleNamespace(
        realtime_client=FakeRealtime(),
        _resolve_entity_by_path=lambda _p, _path: ProjectEntity(
            path="/main.tex", type="doc", entity_id="a" * 24,
        ),
        _invalidate_caches=lambda _p: None,
    )

    result = DocEditor(client).edit(
        "0" * 24, "/main.tex", [{"old": "old", "new": "new"}]
    )

    assert result["message"] == "Edits were already applied (ack recovery)"


def _rollback_client(delete_status: dict) -> SimpleNamespace:
    class FakeRealtime:
        def join_doc_write(self, *_args, **_kwargs) -> None:
            raise OTTransportError("network down")

    return SimpleNamespace(
        realtime_client=FakeRealtime(),
        _resolve_entity_by_path=lambda _p, _path: (_ for _ in ()).throw(
            RuntimeError("not found")
        ),
        _resolve_folder_id_by_path=lambda _p, folder: ("f" * 24, folder),
        _post_json_with_csrf=lambda **_k: HttpResult(
            status_code=200,
            headers=CaseInsensitiveDict(),
            text=json.dumps({"_id": "a" * 24}),
            url="https://overleaf.example/project/doc",
        ),
        _delete_with_csrf=lambda **_k: HttpResult(
            status_code=delete_status["code"],
            headers=CaseInsensitiveDict(),
            text="",
            url="https://overleaf.example/project/doc",
        ),
        _cache_upsert=lambda *_a, **_k: None,
        _invalidate_caches=lambda _p: None,
    )


def test_write_create_rolls_back_orphan_when_insert_fails() -> None:
    client = _rollback_client({"code": 200})

    with pytest.raises(OTTransportError, match="network down"):
        DocEditor(client).write("0" * 24, "/new.tex", "hi")


def test_write_create_rollback_failure_raises_cache_consistency() -> None:
    client = _rollback_client({"code": 500})

    with pytest.raises(CacheConsistencyError):
        DocEditor(client).write("0" * 24, "/new.tex", "hi")


def test_validate_edits_rejects_empty_and_whitespace_and_oversized() -> None:
    editor = DocEditor(SimpleNamespace(realtime_client=SimpleNamespace()))

    with pytest.raises(ParamValidationError):
        editor._validate_edits([])
    with pytest.raises(ParamValidationError):
        editor._validate_edits([{"old": "  ", "new": "x"}])
    with pytest.raises(ParamValidationError):
        editor._validate_edits([{"old": "x" * 20000, "new": "y"}])  # old > 10 KB
    with pytest.raises(ParamValidationError):
        editor._validate_edits([{"old": "x" * 10, "new": "y" * (600 * 1024)}])  # new > 500 KB


def test_join_doc_write_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    join_doc_ack = "6:::2+" + json.dumps([None, ["hello"], 3, [], {}, "sharejs-text-ot"])
    ot_error = "5:::" + json.dumps({"name": "otUpdateError", "args": [{"doc": "a" * 24}]})
    plans = [
        ["1::", '5:::{"name":"joinProjectResponse","args":[null]}', join_doc_ack, ot_error],
        ["1::", '5:::{"name":"joinProjectResponse","args":[null]}', join_doc_ack, "6:::3"],
    ]
    state = {"conn": -1}

    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            state["conn"] += 1
            self._plan = list(plans[min(state["conn"], len(plans) - 1)])
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda _t: None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def recv(self) -> str:
            return self._plan.pop(0)

        def _send_locked(self, _data: str) -> None:
            return None

        def drain_initial_messages(self, _expected_count: int = 1) -> None:
            for _ in range(_expected_count):
                self.recv()

        def send_event_with_ack(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=60),
        SimpleNamespace(),
    )

    client.join_doc_write(
        "0" * 24, "a" * 24, lambda _content: [{"p": 6, "i": "there "}]
    )

    assert state["conn"] == 1  # one conflict, one successful retry


@pytest.mark.timeout(5)
def test_ack_wait_timeout_raises_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            self.calls = 0
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda _t: None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def recv(self) -> str:
            self.calls += 1
            if self.calls == 1:
                return "1::"
            if self.calls == 2:
                return '5:::{"name":"joinProjectResponse","args":[null]}'
            if self.calls == 3:
                return "6:::2+" + json.dumps([None, ["hello"], 3, [], {}, "sharejs-text-ot"])
            raise realtime_module.WebSocketTimeoutError("no ack")

        def _send_locked(self, _data: str) -> None:
            return None

        def drain_initial_messages(self, _expected_count: int = 1) -> None:
            for _ in range(_expected_count):
                self.recv()

        def send_event_with_ack(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=60),
        SimpleNamespace(),
    )

    with pytest.raises(OTTransportError, match="Timed out waiting for ack"):
        client.join_doc_write(
            "0" * 24, "a" * 24, lambda _content: [{"p": 6, "i": "there "}], timeout=0.3
        )


def test_progress_completes_on_empty_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        "1::",
        '5:::{"name":"joinProjectResponse","args":[null]}',
        "6:::2+" + json.dumps([None, ["hello"], 3, [], {}, "sharejs-text-ot"]),
    ]

    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            self._messages = list(messages)
            self.ws = SimpleNamespace()
            self.ws.settimeout = lambda _t: None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def recv(self) -> str:
            return self._messages.pop(0)

        def _send_locked(self, _data: str) -> None:
            return None

        def drain_initial_messages(self, _expected_count: int = 1) -> None:
            for _ in range(_expected_count):
                self.recv()

        def send_event_with_ack(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(realtime_module, "LegacySocketConnection", FakeConnection)
    client = realtime_module.RealtimeProjectClient(
        SimpleNamespace(timeout_seconds=60),
        SimpleNamespace(),
    )

    events: list[tuple[int, int]] = []

    client.join_doc_write(
        "0" * 24, "a" * 24, lambda _content: [],  # empty ops → no OT round-trip
        progress=lambda done, total, _m: events.append((done, total)),
    )

    assert events == [(1, 4), (2, 4), (4, 4)]


def test_per_project_lock_provides_mutual_exclusion() -> None:
    import threading

    client = _make_project_client()
    lock = client._op_lock("0" * 24)
    counter = {"value": 0}
    errors: list[Exception] = []

    def worker() -> None:
        try:
            with lock:
                value = counter["value"]
                time.sleep(0.005)
                counter["value"] = value + 1
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert counter["value"] == 20


def test_join_snapshot_lines_decodes_latin1_bytes_and_strips_crlf() -> None:
    # "测试中文ABC。" as Latin-1-decoded UTF-8 bytes
    mojibake = "æµ\x8bè¯\x95ä¸\xadæ\x96\x87ABCã\x80\x82"
    result = realtime_module._join_snapshot_lines([mojibake + "\r", ""])
    assert result == "测试中文ABC。\n"

    # genuine Unicode must be left untouched (e.g. "café")
    assert realtime_module._join_snapshot_lines(["café", "x"]) == "café\nx"

    # ASCII is unchanged
    assert realtime_module._join_snapshot_lines(["hello", "world"]) == "hello\nworld"
