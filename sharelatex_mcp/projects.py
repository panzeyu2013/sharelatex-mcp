from __future__ import annotations

import copy
import html
import json
import logging
import mimetypes
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from diff_match_patch import diff_match_patch  # package: diff-match-patch (PyPI)
from websocket import WebSocketConnectionClosedException

from sharelatex_mcp.http import HttpResult
from sharelatex_mcp.realtime import RealtimeProjectClient
from sharelatex_mcp.session import OverleafSessionManager
from sharelatex_mcp.validation import (
    normalize_entity_type,
    validate_entity_id,
    validate_path_segment,
    validate_project_id,
)

logger = logging.getLogger(__name__)


def _compute_diff_operations(old: str, new: str) -> list[dict[str, Any]]:
    """Compute minimal sharejs-text-ot operations from old to new content.

    Uses Myers diff algorithm (via diff-match-patch) for guaranteed minimal
    edit distance.  Returns a list of operations that can be sent as a batch
    to ``applyOtUpdate``.  Each operation's position is correct for sequential
    application by the ShareJS OT engine.
    """
    if old == new:
        return []

    dmp = diff_match_patch()
    diffs = dmp.diff_main(old, new)
    dmp.diff_cleanupMerge(diffs)

    ops: list[dict[str, Any]] = []
    position = 0

    for op, text in diffs:
        if not text:
            continue
        if op == 0:  # EQUAL
            position += len(text)
        elif op == -1:  # DELETE
            ops.append({"p": position, "d": text})
        elif op == 1:  # INSERT
            ops.append({"p": position, "i": text})
            position += len(text)

    return ops


@dataclass
class ProjectSummary:
    project_id: str
    name: str
    project_url: str
    archived: bool = False
    trashed: bool = False
    access_level: str | None = None


@dataclass
class ProjectEntity:
    path: str
    type: str
    entity_id: str | None = None
    parent_folder_id: str | None = None
    hash: str | None = None


def _extract_project_id(url: str) -> str | None:
    match = re.search(r"/project/([a-f0-9]{24})", url)
    if match:
        return match.group(1)
    return None


def _extract_line_number(text: str) -> int | None:
    match = re.search(r"\bl\.(\d+)\b", text)
    if match:
        return int(match.group(1))
    return None


def _extract_line_range(text: str) -> tuple[int | None, int | None]:
    match = re.search(r"at lines? (\d+)(?:--(\d+))?", text)
    if not match:
        return None, None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    return start, end


def _build_compile_fix_hint(kind: str, message: str) -> str | None:
    if kind == "missing-file":
        return (
            "Check whether the missing file has been uploaded, and verify the relative path, "
            "case, and extension used in the TeX source."
        )
    if kind == "undefined-control-sequence":
        return (
            "Check for a misspelled command or a missing package. If the command comes from a "
            "package, make sure it is loaded in the preamble."
        )
    if kind == "package-error":
        return (
            "Check the documented usage and argument format of the reported package, and verify "
            "that it does not conflict with the current template."
        )
    if kind == "latex-error":
        return (
            "Look near the reported line for mismatched braces, environment bounds, and command "
            "arguments — LaTeX often reports the error several lines after the actual cause."
        )
    if kind == "citation-warning":
        return (
            "Check that the citation key exists in the .bib file and that the bibliography "
            "toolchain and root doc are configured correctly, then recompile."
        )
    if kind == "reference-warning":
        return (
            "Cross-references usually require 1–2 additional compilations. If warnings persist, "
            "verify that the label names are consistent."
        )
    if kind == "bibtex-warning":
        return (
            "Check the .bib file path, BibTeX keys, and entry formats. If necessary, verify "
            "whether the project should use Biber instead."
        )
    if kind == "box-warning":
        return (
            "This is usually not fatal but affects typesetting. Check long equations, long "
            "words, image widths, or line-breaking settings."
        )
    if kind == "font-warning":
        return (
            "Check that the font package, compilation engine, and font name are compatible, "
            "especially when using XeLaTeX or LuaLaTeX."
        )
    if kind == "package-warning":
        return (
            "Check the package documentation and verify that the package arguments, "
            "version, and engine compatibility are correct for the reported warning."
        )
    if "rerun" in message.lower():
        return "This is a standard two-pass compilation hint — usually resolved by compiling once more."
    return None


class ProjectClient:
    def __init__(self, session_manager: OverleafSessionManager) -> None:
        self.session_manager = session_manager
        self.realtime_client = RealtimeProjectClient(session_manager.config, session_manager)
        self._compile_cache: dict[str, tuple[float, dict[str, object], tuple[object, ...]]] = {}
        self._entity_cache: dict[str, dict[str, ProjectEntity]] = {}
        self._entity_id_index: dict[str, dict[str, str]] = {}
        self._tree_cache: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self.session_manager.close()

    def _invalidate_caches(self, project_id: str) -> None:
        self._compile_cache.pop(project_id, None)
        self._tree_cache.pop(project_id, None)

    def _request_with_csrf_retry(
        self,
        project_id: str,
        request_fn: Callable[[dict[str, str]], HttpResult],
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        project_id = validate_project_id(project_id)
        result: HttpResult | None = None
        for force_refresh in (False, True):
            csrf_token = self.session_manager.get_csrf_token(
                project_id=project_id, force_refresh=force_refresh,
            )
            headers: dict[str, str] = {"X-Csrf-Token": csrf_token}
            if extra_headers:
                headers.update(extra_headers)
            result = request_fn(headers)
            if result.status_code != 403:
                return result
        assert result is not None
        return result

    def _post_json_with_csrf(
        self,
        project_id: str,
        path: str,
        payload: dict[str, object],
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        project_id = validate_project_id(project_id)
        return self._request_with_csrf_retry(
            project_id=project_id,
            request_fn=lambda headers: self.session_manager.http.post_json(
                path, payload=payload, headers=headers,
            ),
            extra_headers=extra_headers,
        )

    def _post_multipart_with_csrf(
        self,
        project_id: str,
        path: str,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        project_id = validate_project_id(project_id)
        result: HttpResult | None = None
        for force_refresh in (False, True):
            csrf_token = self.session_manager.get_csrf_token(
                project_id=project_id, force_refresh=force_refresh,
            )
            headers: dict[str, str] = {"X-Csrf-Token": csrf_token}
            if extra_headers:
                headers.update(extra_headers)
            request_params = dict(params or {})
            request_params["_csrf"] = csrf_token
            result = self.session_manager.http.post_multipart(
                path,
                files=files,
                data=data,
                headers=headers,
                params=request_params,
            )
            if result.status_code != 403:
                return result
        assert result is not None
        return result

    def _delete_with_csrf(
        self,
        project_id: str,
        path: str,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        project_id = validate_project_id(project_id)
        return self._request_with_csrf_retry(
            project_id=project_id,
            request_fn=lambda headers: self.session_manager.http.delete(
                path, headers=headers,
            ),
            extra_headers=extra_headers,
        )

    def _compile_backoff_seconds(self, status: str | None, base_interval_seconds: float) -> float:
        if status in {"too-recently-compiled", "compile-in-progress"}:
            return max(base_interval_seconds, 60.0)
        return base_interval_seconds

    def _build_compile_variants(
        self,
        root_doc_id: str,
        draft: bool,
        stop_on_first_error: bool,
        check: str,
        allow_compat_variants: bool,
    ) -> list[tuple[str, dict[str, object]]]:
        variants: list[tuple[str, dict[str, object]]] = [
            (
                "primary_without_editorid_incremental",
                {
                    "rootDoc_id": root_doc_id,
                    "draft": draft,
                    "check": check,
                    "incrementalCompilesEnabled": True,
                    "stopOnFirstError": stop_on_first_error,
                },
            )
        ]
        if not allow_compat_variants:
            return variants

        variants.extend(
            [
                (
                    "compat_without_editorid_nonincremental",
                    {
                        "rootDoc_id": root_doc_id,
                        "draft": draft,
                        "check": check,
                        "incrementalCompilesEnabled": False,
                        "stopOnFirstError": stop_on_first_error,
                    },
                ),
            ]
        )
        return variants

    def _summarize_compile_payload(self, payload: dict[str, object]) -> dict[str, object]:
        return {
            "has_root_doc_id": bool(payload.get("rootDoc_id")),
            "draft": payload.get("draft"),
            "check": payload.get("check"),
            "incrementalCompilesEnabled": payload.get("incrementalCompilesEnabled"),
            "stopOnFirstError": payload.get("stopOnFirstError"),
            "has_editor_id": "editorId" in payload,
        }

    def _map_entity_type(self, entity_type: str) -> str:
        return normalize_entity_type(entity_type)

    def _get_cached_entity(self, project_id: str, path: str) -> ProjectEntity | None:
        return self._entity_cache.get(project_id, {}).get(path)

    def _cache_entities(self, project_id: str, entities: list[ProjectEntity]) -> None:
        self._entity_cache[project_id] = {entity.path: entity for entity in entities}
        id_index: dict[str, str] = {}
        for entity in entities:
            if entity.entity_id:
                id_index[entity.entity_id] = entity.path
            if entity.hash:
                id_index[entity.hash] = entity.path
        self._entity_id_index[project_id] = id_index

    def _cache_upsert(self, project_id: str, entity: ProjectEntity) -> None:
        project_cache = self._entity_cache.setdefault(project_id, {})
        existing = project_cache.get(entity.path)
        index = self._entity_id_index.setdefault(project_id, {})
        if existing is not None and existing.entity_id != entity.entity_id:
            if existing.entity_id and existing.entity_id in index:
                del index[existing.entity_id]
            if existing.hash and existing.hash in index:
                del index[existing.hash]
        project_cache[entity.path] = entity
        if entity.entity_id:
            index[entity.entity_id] = entity.path
        if entity.hash:
            index[entity.hash] = entity.path

    def _cache_delete_by_path(self, project_id: str, path: str) -> None:
        project_cache = self._entity_cache.get(project_id)
        if project_cache is None:
            return
        entity = project_cache.pop(path, None)
        if entity is not None:
            idx = self._entity_id_index.get(project_id, {})
            if entity.entity_id and entity.entity_id in idx:
                del idx[entity.entity_id]
            if entity.hash and entity.hash in idx:
                del idx[entity.hash]

    def _cache_delete_by_entity_id(self, project_id: str, entity_id: str) -> None:
        idx = self._entity_id_index.get(project_id, {})
        path = idx.get(entity_id)
        if path is not None:
            entity = self._entity_cache.get(project_id, {}).pop(path, None)
            del idx[entity_id]
            if entity is not None and entity.hash and entity.hash in idx:
                del idx[entity.hash]

    def _resolve_entity_by_path(self, project_id: str, path: str) -> ProjectEntity:
        cached = self._get_cached_entity(project_id, path)
        if cached is not None:
            return cached
        entities = self.list_files_with_ids(project_id)
        target = next((entity for entity in entities if entity.path == path), None)
        if target is None:
            raise RuntimeError(f"Entity not found in project: {path}")
        return target

    def _default_download_output_path(self, project_id: str, path: str) -> str:
        relative_path = path.lstrip("/")
        return os.path.abspath(os.path.join("downloads", project_id, relative_path))

    def list_projects(self) -> list[ProjectSummary]:
        self.session_manager.ensure_logged_in()

        candidates = ["/project", "/"]
        for path in candidates:
            logger.debug("Listing projects from %s", path)
            result = self.session_manager.http.get(path)
            if result.status_code >= 400:
                continue

            projects = list(self._parse_projects_from_html(result.text))
            if projects:
                return projects

        return []

    def open_project(self, project_id: str) -> dict[str, str | int | None]:
        project_id = validate_project_id(project_id)
        self.session_manager.ensure_logged_in()
        logger.info("Opening project %s", project_id)
        result = self.session_manager.http.get(f"/project/{project_id}")
        title = self._extract_title(result.text)
        return {
            "project_id": project_id,
            "status": result.status_code,
            "title": title,
            "html_snippet": result.text[:400],
        }

    def get_project_diagnostics(self, project_id: str) -> dict[str, object]:
        project_id = validate_project_id(project_id)
        self.session_manager.ensure_logged_in()
        logger.info("Reading diagnostics for project %s", project_id)
        result = self.session_manager.http.get(f"/project/{project_id}")
        if result.status_code != 200:
            raise RuntimeError(f"Failed to read project page, status code: {result.status_code}")

        soup = BeautifulSoup(result.text, "html.parser")
        exposed_settings = self._read_meta_dict(soup, "ol-ExposedSettings")
        user = self._read_meta_dict(soup, "ol-user")
        raw_user_features = user.get("features", {})
        user_features = raw_user_features if isinstance(raw_user_features, dict) else {}
        compile_settings = self._read_meta_dict(soup, "ol-compileSettings")
        user_settings = self._read_meta_dict(soup, "ol-userSettings")
        capabilities = self._read_meta_list(soup, "ol-capabilities")
        project_tags = self._read_meta_list(soup, "ol-projectTags")

        return {
            "project_id": project_id,
            "project_name": self._read_meta_content(soup, "ol-projectName"),
            "compile": {
                "compile_timeout": compile_settings.get("compileTimeout"),
                "compile_group": user_features.get("compileGroup"),
                "can_use_clsi_cache": soup.find("meta", attrs={"name": "ol-canUseClsiCache"}) is not None,
                "compiles_user_content_domain": self._read_meta_content(soup, "ol-compilesUserContentDomain"),
            },
            "features": {
                "git_bridge": user_features.get("gitBridge"),
                "references": user_features.get("references"),
                "track_changes": user_features.get("trackChanges"),
                "versioning": user_features.get("versioning"),
                "dropbox": user_features.get("dropbox"),
                "compile_timeout": user_features.get("compileTimeout"),
            },
            "editor": {
                "pdf_viewer": user_settings.get("pdfViewer"),
                "syntax_validation": user_settings.get("syntaxValidation"),
                "font_size": user_settings.get("fontSize"),
                "auto_complete": user_settings.get("autoComplete"),
            },
            "capabilities": capabilities,
            "project_tags": [tag.get("name") for tag in project_tags if isinstance(tag, dict) and tag.get("name")],
            "server": {
                "app_name": exposed_settings.get("appName"),
                "is_overleaf": exposed_settings.get("isOverleaf"),
                "has_linked_project_file_feature": exposed_settings.get("hasLinkedProjectFileFeature"),
                "has_linked_project_output_file_feature": exposed_settings.get("hasLinkedProjectOutputFileFeature"),
            },
        }

    def list_files(self, project_id: str) -> list[ProjectEntity]:
        project_id = validate_project_id(project_id)
        self.session_manager.ensure_logged_in()
        logger.debug("Listing files for project %s via HTTP entities endpoint", project_id)
        result = self.session_manager.http.get(f"/project/{project_id}/entities")
        if result.status_code != 200:
            raise RuntimeError(f"Failed to read project file tree, status code: {result.status_code}")

        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as err:
            raise RuntimeError("Failed to parse project file tree: invalid JSON response") from err

        entities = payload.get("entities", [])
        return [
            ProjectEntity(
                path=entity["path"],
                type=entity["type"],
            )
            for entity in entities
            if "path" in entity and "type" in entity
        ]

    def get_project_tree(self, project_id: str) -> dict[str, Any]:
        project_id = validate_project_id(project_id)
        if project_id in self._tree_cache:
            return self._tree_cache[project_id]

        attempts = 3
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                joined = self.realtime_client.join_project(project_id)
                self._tree_cache[project_id] = joined.project
                return joined.project
            except (WebSocketConnectionClosedException, RuntimeError) as exc:
                last_error = exc
                logger.warning(
                    "WebSocket failure (attempt %d/%d) for project %s: %s",
                    attempt, attempts, project_id, exc,
                )
                if attempt == attempts:
                    break
                time.sleep(0.5 * attempt)
        if last_error is not None:
            raise RuntimeError(
                f"Failed to read project tree: websocket connection closed after {attempts} retries"
            ) from last_error
        raise RuntimeError(
            f"Failed to read project tree: no valid response after {attempts} attempts"
        )

    def get_root_doc(self, project_id: str) -> dict[str, str | None]:
        project_id = validate_project_id(project_id)
        project = self.get_project_tree(project_id)
        root_doc_id = project.get("rootDoc_id")
        if not root_doc_id:
            return {
                "project_id": project_id,
                "root_doc_id": None,
                "root_doc_path": None,
            }

        entities = self.list_files_with_ids(project_id)
        target = next((entity for entity in entities if entity.type == "doc" and entity.entity_id == root_doc_id), None)
        return {
            "project_id": project_id,
            "root_doc_id": root_doc_id,
            "root_doc_path": target.path if target else None,
        }

    def set_root_doc(self, project_id: str, path: str) -> dict[str, str | bool | None]:
        project_id = validate_project_id(project_id)
        target = self._resolve_entity_by_path(project_id, path)
        if target.type != "doc":
            raise RuntimeError("set_root_doc currently only supports doc-type text files as the main compile target")
        if not target.entity_id:
            raise RuntimeError(f"Unable to find document ID: {path}")
        root_doc_id = validate_entity_id(target.entity_id, "root_doc_id")

        logger.info("Setting root doc for project %s to %s", project_id, path)
        previous = self.get_root_doc(project_id)
        result = self._post_json_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/settings",
            payload={"rootDocId": root_doc_id},
            extra_headers={"Accept": "application/json"},
        )
        if result.status_code >= 400:
            raise RuntimeError(f"Failed to set root doc, status code: {result.status_code}")

        self._invalidate_caches(project_id)
        current = self.get_root_doc(project_id)
        return {
            "ok": True,
            "project_id": project_id,
            "previous_root_doc_id": previous.get("root_doc_id"),
            "previous_root_doc_path": previous.get("root_doc_path"),
            "root_doc_id": current.get("root_doc_id"),
            "root_doc_path": current.get("root_doc_path"),
            "changed": previous.get("root_doc_id") != current.get("root_doc_id"),
        }

    def create_folder(
        self,
        project_id: str,
        name: str,
        parent_folder_id: str | None = None,
    ) -> dict[str, str]:
        project_id = validate_project_id(project_id)
        if parent_folder_id is not None:
            parent_folder_id = validate_entity_id(parent_folder_id, "parent_folder_id")
        project = self.get_project_tree(project_id)
        root_folders = project.get("rootFolder", [])
        if not root_folders:
            raise RuntimeError("Project missing rootFolder, unable to create folder")

        target_folder_id = parent_folder_id or root_folders[0].get("_id")
        if not target_folder_id:
            raise RuntimeError("Unable to determine target parent folder ID")

        parent_path = self._resolve_folder_path(project_id, target_folder_id)
        logger.info("Creating folder '%s' in project %s (parent: %s)", name, project_id, target_folder_id)
        result = self._post_json_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/folder",
            payload={
                "parent_folder_id": target_folder_id,
                "name": name,
            },
            extra_headers={"Accept": "application/json"},
        )
        if result.status_code >= 400:
            raise RuntimeError(f"Failed to create folder, status code: {result.status_code}")

        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as err:
            raise RuntimeError("Failed to create folder: invalid JSON response") from err

        entity_id = payload.get("_id")
        if not entity_id:
            raise RuntimeError("Folder created but no folder ID returned")

        folder_path = f"{parent_path}/{name}" if parent_path else f"/{name}"
        self._cache_upsert(
            project_id,
            ProjectEntity(
                path=folder_path,
                type="folder",
                entity_id=entity_id,
                parent_folder_id=target_folder_id,
            ),
        )
        self._invalidate_caches(project_id)
        return {
            "project_id": project_id,
            "entity_id": entity_id,
            "path": folder_path,
        }

    def create_doc(self, project_id: str, name: str, parent_folder_id: str | None = None) -> dict[str, str]:
        project_id = validate_project_id(project_id)
        if parent_folder_id is not None:
            parent_folder_id = validate_entity_id(parent_folder_id, "parent_folder_id")
        project = self.get_project_tree(project_id)
        root_folders = project.get("rootFolder", [])
        if not root_folders:
            raise RuntimeError("Project missing rootFolder, unable to create document")

        target_folder_id = parent_folder_id or root_folders[0].get("_id")
        if not target_folder_id:
            raise RuntimeError("Unable to determine target parent folder ID")

        parent_path = self._resolve_folder_path(project_id, target_folder_id)
        logger.info("Creating doc '%s' in project %s (parent: %s)", name, project_id, target_folder_id)
        result = self._post_json_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/doc",
            payload={
                "parent_folder_id": target_folder_id,
                "name": name,
            },
            extra_headers={"Accept": "application/json"},
        )
        if result.status_code >= 400:
            raise RuntimeError(f"Failed to create document, status code: {result.status_code}")

        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as err:
            raise RuntimeError("Failed to create document: invalid JSON response") from err

        entity_id = payload.get("_id")
        if not entity_id:
            raise RuntimeError("Document created but no document ID returned")

        doc_path = f"{parent_path}/{name}" if parent_path else f"/{name}"
        self._cache_upsert(
            project_id,
            ProjectEntity(
                path=doc_path,
                type="doc",
                entity_id=entity_id,
                parent_folder_id=target_folder_id,
            ),
        )
        self._invalidate_caches(project_id)
        return {
            "project_id": project_id,
            "entity_id": entity_id,
            "path": doc_path,
        }

    def compile_project(
        self,
        project_id: str,
        root_doc_id: str | None = None,
        draft: bool = False,
        stop_on_first_error: bool = False,
        check: str = "silent",
        retry_on_500: int = 0,
        retry_delay_seconds: float = 1.0,
        min_interval_seconds: float = 15.0,
        force: bool = False,
        allow_compat_variants: bool = False,
        return_attempt_trace: bool = False,
    ) -> dict[str, object]:
        project_id = validate_project_id(project_id)
        if root_doc_id is not None:
            resolved_root_doc_id = validate_entity_id(root_doc_id, "root_doc_id")
        else:
            project = self.get_project_tree(project_id)
            project_root_doc_id = project.get("rootDoc_id")
            if not isinstance(project_root_doc_id, str):
                raise RuntimeError("Unable to determine rootDoc_id, cannot initiate compilation")
            resolved_root_doc_id = validate_entity_id(project_root_doc_id, "rootDoc_id")

        cached = self._compile_cache.get(project_id)
        now = time.time()
        params_key = (resolved_root_doc_id, draft, check, stop_on_first_error, allow_compat_variants)
        if not force and cached is not None:
            cached_at, cached_payload, cached_params = cached
            if cached_params != params_key:
                logger.debug("Compile cache params mismatch, ignoring cache")
            else:
                cached_status = cached_payload.get("status")
                cached_status_text = cached_status if isinstance(cached_status, str) else None
                cooldown_seconds = self._compile_backoff_seconds(cached_status_text, min_interval_seconds)
                cooldown_status = {"too-recently-compiled", "compile-in-progress"}
                if cached_status_text in cooldown_status and now - cached_at < cooldown_seconds:
                    reused: dict[str, object] = dict(cached_payload)
                    reused["cached"] = True
                    reused["cache_age_seconds"] = round(now - cached_at, 3)
                    reused["local_skip_reason"] = "recent_compile_cooldown"
                    reused["cooldown_seconds"] = cooldown_seconds
                    if return_attempt_trace and "attempt_trace" not in reused:
                        reused["attempt_trace"] = []
                    return reused

        compile_variants = self._build_compile_variants(
            root_doc_id=resolved_root_doc_id,
            draft=draft,
            stop_on_first_error=stop_on_first_error,
            check=check,
            allow_compat_variants=allow_compat_variants,
        )

        result = None
        attempts_made = 0
        attempt_trace: list[dict[str, object]] = []
        selected_variant_label: str | None = None
        for variant_label, compile_request_payload in compile_variants:
            attempts = max(1, retry_on_500 + 1)
            for attempt in range(1, attempts + 1):
                attempts_made += 1
                started_at = time.time()
                logger.info(
                    "Compiling project %s [variant=%s, attempt=%d/%d]",
                    project_id, variant_label, attempt, attempts,
                )
                result = self._post_json_with_csrf(
                    project_id=project_id,
                    path=f"/project/{project_id}/compile",
                    payload=compile_request_payload,
                    extra_headers={"Accept": "application/json"},
                )
                body_snippet = result.text.replace("\n", " ")[:200]
                trace_entry: dict[str, object] = {
                    "variant": variant_label,
                    "global_attempt": attempts_made,
                    "attempt_in_variant": attempt,
                    "status_code": result.status_code,
                    "duration_seconds": round(time.time() - started_at, 3),
                    "payload": self._summarize_compile_payload(compile_request_payload),
                    "body_snippet": body_snippet,
                }
                if result.status_code == 200:
                    try:
                        trace_entry["response_status"] = json.loads(result.text).get("status")
                    except json.JSONDecodeError:
                        trace_entry["response_status"] = "non-json-200"
                attempt_trace.append(trace_entry)
                if result.status_code == 200:
                    selected_variant_label = variant_label
                    break
                if result.status_code >= 500 and attempt < attempts:
                    logger.warning(
                        "Compile attempt %d returned 500, retrying after %.1fs",
                        attempts_made, retry_delay_seconds,
                    )
                    time.sleep(retry_delay_seconds)
                    continue
                break
            if result is not None and result.status_code == 200:
                if allow_compat_variants:
                    try:
                        response_status = json.loads(result.text).get("status")
                    except json.JSONDecodeError:
                        response_status = None
                    if response_status not in ("success",):
                        selected_variant_label = selected_variant_label if selected_variant_label else variant_label
                        logger.info(
                            "HTTP 200 with status=%s, trying compat variant next",
                            response_status,
                        )
                        continue
                break

        if result is None:
            raise RuntimeError("Unable to issue compile request")

        if result.status_code != 200:
            return {
                "ok": False,
                "project_id": project_id,
                "rootDoc_id": resolved_root_doc_id,
                "status_code": result.status_code,
                "attempts": attempts_made,
                "body_snippet": result.text[:500],
                "selected_variant": selected_variant_label,
                "attempt_trace": attempt_trace if return_attempt_trace else None,
            }

        try:
            response_payload: dict[str, object] = json.loads(result.text)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "project_id": project_id,
                "rootDoc_id": resolved_root_doc_id,
                "status_code": result.status_code,
                "body_snippet": result.text[:500],
                "message": "Compile endpoint returned a non-JSON response",
                "selected_variant": selected_variant_label,
                "attempt_trace": attempt_trace if return_attempt_trace else None,
            }

        response_payload["ok"] = True
        response_payload["project_id"] = project_id
        response_payload["rootDoc_id"] = resolved_root_doc_id
        response_payload["attempts"] = attempts_made
        response_payload["cached"] = False
        response_status = response_payload.get("status")
        response_payload["cooldown_seconds"] = self._compile_backoff_seconds(
            response_status if isinstance(response_status, str) else None,
            min_interval_seconds,
        )
        response_payload["selected_variant"] = selected_variant_label
        if return_attempt_trace:
            response_payload["attempt_trace"] = attempt_trace
        self._compile_cache[project_id] = (time.time(), copy.deepcopy(response_payload), params_key)
        return response_payload

    def stop_compile(self, project_id: str) -> dict[str, object]:
        project_id = validate_project_id(project_id)
        logger.info("Stopping compile for project %s", project_id)
        result = self._post_json_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/compile/stop",
            payload={},
            extra_headers={"Accept": "application/json"},
        )
        self._invalidate_caches(project_id)
        response: dict[str, object] = {
            "ok": result.status_code == 200,
            "project_id": project_id,
            "status_code": result.status_code,
        }
        try:
            response["payload"] = json.loads(result.text) if result.text else {}
        except json.JSONDecodeError:
            response["body_snippet"] = result.text[:500]
        return response

    def clear_compile_output(self, project_id: str) -> dict[str, object]:
        project_id = validate_project_id(project_id)
        logger.info("Clearing compile output for project %s", project_id)
        result = self._delete_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/output",
            extra_headers={"Accept": "application/json"},
        )
        self._invalidate_caches(project_id)
        response: dict[str, object] = {
            "ok": result.status_code == 200,
            "project_id": project_id,
            "status_code": result.status_code,
        }
        try:
            response["payload"] = json.loads(result.text) if result.text else {}
        except json.JSONDecodeError:
            response["body_snippet"] = result.text[:500]
        return response

    def get_compile_logs(
        self,
        project_id: str,
        compile_result: dict[str, object] | None = None,
        max_bytes: int = 200_000,
        trigger_compile_if_missing: bool = False,
    ) -> dict[str, object]:
        project_id = validate_project_id(project_id)
        if compile_result is None:
            if not trigger_compile_if_missing:
                return {
                    "ok": False,
                    "project_id": project_id,
                    "status": "compile-not-requested",
                    "message": (
                        "No compile_result provided. To avoid triggering unexpected compilations, "
                        "call compile_project first, or pass trigger_compile_if_missing=True."
                    ),
                }
            compile_payload = self.compile_project(project_id)
        else:
            compile_payload = compile_result
        output_files_payload = compile_payload.get("outputFiles")
        output_files = output_files_payload if isinstance(output_files_payload, list) else []
        if not output_files:
            return {
                "ok": False,
                "project_id": project_id,
                "status": compile_payload.get("status"),
                "message": "No outputFiles available — compilation may not have produced logs yet.",
                "compile_result": compile_payload,
            }

        log_file = next(
            (
                item for item in output_files
                if isinstance(item, dict) and item.get("path") == "output.log"
            ),
            None,
        )
        bib_logs = [
            item for item in output_files
            if isinstance(item, dict) and str(item.get("path", "")).endswith(".blg")
        ]

        result: dict[str, object] = {
            "ok": True,
            "project_id": project_id,
            "status": compile_payload.get("status"),
            "available_output_files": [
                item.get("path") for item in output_files if isinstance(item, dict)
            ],
            "output_log": None,
            "bib_logs": [],
        }

        if isinstance(log_file, dict):
            try:
                result["output_log"] = self._fetch_output_file_text(
                    log_file,
                    compile_payload=compile_payload,
                    max_bytes=max_bytes,
                )
            except Exception as exc:
                logger.warning("Failed to fetch output.log: %s", exc)
                result["output_log"] = f"<error reading output.log: {exc}>"

        bib_log_payloads: list[dict[str, object | str | None]] = []
        for item in bib_logs:
            content: str | None
            try:
                content = self._fetch_output_file_text(
                    item,
                    compile_payload=compile_payload,
                    max_bytes=max_bytes,
                )
            except Exception as exc:
                logger.warning("Failed to fetch bib log %s: %s", item.get("path"), exc)
                content = f"<error reading {item.get('path')}: {exc}>"
            bib_log_payloads.append(
                {
                    "path": item.get("path"),
                    "content": content,
                }
            )
        result["bib_logs"] = bib_log_payloads
        return result

    def analyze_compile_errors(
        self,
        project_id: str,
        compile_result: dict[str, object] | None = None,
        max_bytes: int = 200_000,
        trigger_compile_if_missing: bool = False,
    ) -> dict[str, object]:
        project_id = validate_project_id(project_id)
        logs_payload = self.get_compile_logs(
            project_id=project_id,
            compile_result=compile_result,
            max_bytes=max_bytes,
            trigger_compile_if_missing=trigger_compile_if_missing,
        )
        if not logs_payload.get("ok"):
            return {
                "ok": False,
                "project_id": project_id,
                "status": logs_payload.get("status"),
                "message": logs_payload.get("message"),
                "logs_result": logs_payload,
                "summary": {
                    "has_errors": False,
                    "error_count": 0,
                    "warning_count": 0,
                    "info_count": 0,
                },
                "diagnostics": [],
            }

        diagnostics = self._parse_compile_logs(logs_payload)
        error_count = sum(1 for item in diagnostics if item["severity"] == "error")
        warning_count = sum(1 for item in diagnostics if item["severity"] == "warning")
        info_count = sum(1 for item in diagnostics if item["severity"] == "info")
        primary_issue = next((item for item in diagnostics if item["severity"] == "error"), None)
        if primary_issue is None:
            primary_issue = next((item for item in diagnostics if item["severity"] == "warning"), None)

        return {
            "ok": True,
            "project_id": project_id,
            "status": logs_payload.get("status"),
            "summary": {
                "has_errors": error_count > 0,
                "error_count": error_count,
                "warning_count": warning_count,
                "info_count": info_count,
                "primary_issue": primary_issue,
            },
            "diagnostics": diagnostics,
            "available_output_files": logs_payload.get("available_output_files", []),
        }

    def get_compile_artifacts(
        self,
        project_id: str,
        compile_result: dict[str, object] | None = None,
        trigger_compile_if_missing: bool = False,
    ) -> dict[str, object]:
        project_id = validate_project_id(project_id)
        if compile_result is None:
            if not trigger_compile_if_missing:
                return {
                    "ok": False,
                    "project_id": project_id,
                    "status": "compile-not-requested",
                    "message": (
                        "No compile_result provided. To avoid triggering unexpected compilations, "
                        "call compile_project first, or pass trigger_compile_if_missing=True."
                    ),
                    "artifacts": [],
                    "pdf": None,
                }
            compile_payload = self.compile_project(project_id)
        else:
            compile_payload = compile_result
        output_files_payload = compile_payload.get("outputFiles")
        output_files = output_files_payload if isinstance(output_files_payload, list) else []
        if not output_files:
            return {
                "ok": False,
                "project_id": project_id,
                "status": compile_payload.get("status"),
                "message": "No compile artifacts available.",
                "compile_result": compile_payload,
                "artifacts": [],
                "pdf": None,
            }

        artifacts: list[dict[str, object | str | None]] = []
        pdf_artifact = None
        for item in output_files:
            if not isinstance(item, dict):
                continue
            try:
                resolved_url = self._resolve_output_file_url(item, compile_payload=compile_payload)
            except RuntimeError as exc:
                logger.warning("Failed to resolve artifact URL for %s: %s", item.get("path"), exc)
                resolved_url = None
            artifact: dict[str, object | str | None] = {
                "path": item.get("path"),
                "type": item.get("type"),
                "build": item.get("build"),
                "resolved_url": resolved_url,
            }
            artifacts.append(artifact)
            if item.get("path") == "output.pdf":
                pdf_artifact = artifact

        return {
            "ok": True,
            "project_id": project_id,
            "status": compile_payload.get("status"),
            "artifacts": artifacts,
            "pdf": pdf_artifact,
        }

    def download_pdf(
        self,
        project_id: str,
        compile_result: dict[str, object] | None = None,
        output_path: str | None = None,
        trigger_compile_if_missing: bool = False,
    ) -> dict[str, object]:
        project_id = validate_project_id(project_id)
        artifacts = self.get_compile_artifacts(
            project_id,
            compile_result,
            trigger_compile_if_missing=trigger_compile_if_missing,
        )
        if not artifacts.get("ok"):
            return {
                "ok": False,
                "project_id": project_id,
                "message": "No PDF artifact available for download.",
                "artifacts_result": artifacts,
            }

        pdf_artifact = artifacts.get("pdf")
        if not pdf_artifact or not isinstance(pdf_artifact, dict):
            return {
                "ok": False,
                "project_id": project_id,
                "message": "No output.pdf found in compile artifacts.",
                "artifacts_result": artifacts,
            }

        resolved_url = pdf_artifact["resolved_url"]
        if not isinstance(resolved_url, str):
            return {
                "ok": False,
                "project_id": project_id,
                "message": "Unable to resolve PDF download URL.",
                "artifacts_result": artifacts,
            }

        binary_result = self.session_manager.http.get_bytes_absolute(resolved_url)
        if binary_result.status_code != 200:
            return {
                "ok": False,
                "project_id": project_id,
                "message": "Failed to download PDF.",
                "status_code": binary_result.status_code,
                "resolved_url": resolved_url,
            }

        content_type = binary_result.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            return {
                "ok": False,
                "project_id": project_id,
                "message": (
                    "Download returned HTML instead of PDF — "
                    "the session may have expired or the compile output is no longer accessible."
                ),
                "content_type": content_type,
                "resolved_url": resolved_url,
            }

        if not output_path:
            output_path = os.path.abspath(f"downloads/{project_id}-output.pdf")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as fh:
            fh.write(binary_result.content)

        logger.info(
            "Downloaded PDF for project %s to %s (%d bytes)",
            project_id, output_path, len(binary_result.content),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "output_path": output_path,
            "bytes": len(binary_result.content),
            "resolved_url": resolved_url,
        }

    def list_files_with_ids(self, project_id: str) -> list[ProjectEntity]:
        project_id = validate_project_id(project_id)
        project = self.get_project_tree(project_id)
        root_folders = project.get("rootFolder", [])
        collected: list[ProjectEntity] = []
        for folder in root_folders:
            self._collect_entities_from_folder(
                folder=folder, parent_path="", parent_folder_id=None, output=collected,
            )
        self._cache_entities(project_id, collected)
        return collected

    def read_file(self, project_id: str, path: str) -> dict[str, str]:
        project_id = validate_project_id(project_id)
        target = self._resolve_entity_by_path(project_id, path)
        logger.info("Reading file %s in project %s", path, project_id)

        if target.type == "doc":
            if not target.entity_id:
                raise RuntimeError(f"Unable to find document ID: {path}")
            doc_id = validate_path_segment(target.entity_id, "doc_id")
            result = self.session_manager.http.get(f"/project/{project_id}/doc/{doc_id}/download")
            if result.status_code != 200:
                raise RuntimeError(f"Failed to read document, status code: {result.status_code}")
            return {
                "project_id": project_id,
                "path": path,
                "type": target.type,
                "content": result.text,
            }

        if target.type == "fileRef":
            raise RuntimeError(
                "Reading binary fileRef content as text is not supported in the current version. "
                "Please use download_file for binary resources."
            )

        raise RuntimeError(f"Unsupported file type: {target.type}")

    def download_file(
        self, project_id: str, path: str, output_path: str | None = None,
    ) -> dict[str, str | int | bool | None]:
        project_id = validate_project_id(project_id)
        target = self._resolve_entity_by_path(project_id, path)
        logger.info("Downloading file %s from project %s", path, project_id)
        resolved_url: str

        if target.type == "doc":
            if not target.entity_id:
                raise RuntimeError(f"Unable to find document ID: {path}")
            doc_id = validate_path_segment(target.entity_id, "doc_id")
            resolved_url = urljoin(
                self.session_manager.config.base_url.rstrip("/") + "/",
                f"project/{project_id}/doc/{doc_id}/download",
            )
            binary_result = self.session_manager.http.get_bytes(
                f"/project/{project_id}/doc/{doc_id}/download"
            )
        elif target.type == "fileRef":
            if not target.hash:
                raise RuntimeError(f"Unable to find fileRef hash: {path}")
            file_hash = validate_path_segment(target.hash, "file_hash")
            resolved_url = urljoin(
                self.session_manager.config.base_url.rstrip("/") + "/",
                f"project/{project_id}/blob/{file_hash}",
            )
            binary_result = self.session_manager.http.get_bytes(
                f"/project/{project_id}/blob/{file_hash}"
            )
        else:
            raise RuntimeError(f"Currently only doc and fileRef download is supported, got: {target.type}")

        if binary_result.status_code != 200:
            return {
                "ok": False,
                "project_id": project_id,
                "path": path,
                "entity_type": target.type,
                "message": "Failed to download file.",
                "status_code": binary_result.status_code,
                "resolved_url": resolved_url,
            }

        if not output_path:
            output_path = self._default_download_output_path(project_id, path)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as fh:
            fh.write(binary_result.content)

        logger.info("Downloaded %s → %s (%d bytes)", path, output_path, len(binary_result.content))
        return {
            "ok": True,
            "project_id": project_id,
            "path": path,
            "entity_type": target.type,
            "output_path": output_path,
            "bytes": len(binary_result.content),
            "resolved_url": resolved_url,
        }

    def upload_file(
        self,
        project_id: str,
        local_path: str,
        target_folder_path: str = "/",
        new_name: str | None = None,
    ) -> dict[str, str | int | bool | dict | list | None]:
        project_id = validate_project_id(project_id)
        local_path = os.path.abspath(local_path)
        if not os.path.isfile(local_path):
            raise RuntimeError(f"Local file does not exist: {local_path}")

        upload_name = new_name or os.path.basename(local_path)
        if not upload_name or "/" in upload_name:
            raise RuntimeError("new_name is invalid: must not be empty and must not contain slashes")

        folder_id, normalized_folder_path = self._resolve_folder_id_by_path(project_id, target_folder_path)
        mime_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"

        with open(local_path, "rb") as fh:
            file_bytes = fh.read()

        logger.info(
            "Uploading %s (%d bytes) to project %s:%s",
            upload_name, len(file_bytes), project_id, normalized_folder_path or "/",
        )
        result = self._post_multipart_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/upload",
            files={"qqfile": (upload_name, file_bytes, mime_type)},
            data={"name": upload_name},
            extra_headers={
                "Accept": "application/json",
                "Referer": f"{self.session_manager.config.base_url.rstrip('/')}/project/{project_id}",
            },
            params={
                "folder_id": folder_id,
            },
        )

        response_payload: dict[str, object] | list[Any] | None = None
        if result.text:
            try:
                response_payload = json.loads(result.text)
            except json.JSONDecodeError:
                response_payload = None

        if result.status_code >= 400:
            message = "Failed to upload file."
            if isinstance(response_payload, dict) and response_payload.get("error"):
                message = f"{message} error={response_payload['error']}"
            raise RuntimeError(f"{message} status code: {result.status_code}")

        if not 200 <= result.status_code < 300:
            raise RuntimeError(
                f"Upload returned unexpected status code {result.status_code}. "
                f"The session may have expired (redirect to login)."
            )

        self._invalidate_caches(project_id)
        uploaded_path = f"{normalized_folder_path}/{upload_name}" if normalized_folder_path else f"/{upload_name}"
        uploaded_entity: ProjectEntity | None = None
        retry_delays = [0.5, 1.0, 2.0, 3.0, 5.0, 5.0, 5.0, 5.0]
        for delay in retry_delays:
            time.sleep(delay) if delay else None
            try:
                uploaded_entity = self._resolve_entity_by_path(project_id, uploaded_path)
            except RuntimeError:
                uploaded_entity = None
            if uploaded_entity is not None:
                break

        if uploaded_entity is None and isinstance(response_payload, dict):
            entity_id = response_payload.get("entity_id")
            file_hash = response_payload.get("hash")
            uploaded_entity = ProjectEntity(
                path=uploaded_path,
                type="fileRef",
                entity_id=entity_id if isinstance(entity_id, str) else None,
                parent_folder_id=folder_id,
                hash=file_hash if isinstance(file_hash, str) else None,
            )
        if uploaded_entity is not None:
            self._cache_upsert(project_id, uploaded_entity)

        return {
            "ok": True,
            "project_id": project_id,
            "local_path": local_path,
            "target_folder_path": normalized_folder_path or "/",
            "uploaded_path": uploaded_path,
            "entity_id": uploaded_entity.entity_id if uploaded_entity else None,
            "entity_type": uploaded_entity.type if uploaded_entity else None,
            "bytes": len(file_bytes),
            "content_type": mime_type,
            "server_response": response_payload,
        }

    def replace_file(
        self,
        project_id: str,
        path: str,
        local_path: str,
        new_name: str | None = None,
    ) -> dict[str, str | int | bool | None]:
        project_id = validate_project_id(project_id)
        target = self._resolve_entity_by_path(project_id, path)
        if target.type != "fileRef":
            raise RuntimeError("replace_file currently only supports replacing fileRef binary resources")
        if not target.entity_id:
            raise RuntimeError(f"Unable to find fileRef ID: {path}")

        logger.info("Replacing file %s in project %s", path, project_id)
        final_name = new_name or path.rsplit("/", 1)[-1]
        if not final_name or "/" in final_name:
            raise RuntimeError("new_name is invalid: must not be empty and must not contain slashes")

        parent_path = path.rsplit("/", 1)[0]
        target_folder_path = parent_path or "/"
        backup_name = f"codex-replace-backup-{int(time.time())}-{path.rsplit('/', 1)[-1]}"
        backup_path = f"{parent_path}/{backup_name}" if parent_path else f"/{backup_name}"

        self.rename_entity(project_id=project_id, path=path, new_name=backup_name)

        uploaded_payload: dict[str, str | int | bool | dict | list | None] | None = None
        rollback_failed = False
        try:
            uploaded_payload = self.upload_file(
                project_id=project_id,
                local_path=local_path,
                target_folder_path=target_folder_path,
                new_name=final_name,
            )
        except Exception:
            logger.warning("Upload failed during replace, attempting to restore backup %s → %s", backup_path, path)
            try:
                self.rename_entity(
                    project_id=project_id, path=backup_path, new_name=path.rsplit("/", 1)[-1],
                )
            except Exception as rollback_exc:
                rollback_failed = True
                logger.error(
                    "CRITICAL: Failed to restore backup after upload failure. "
                    "Original file was renamed to %s. Rollback error: %s",
                    backup_path, rollback_exc,
                )
            raise

        delete_backup_error: str | None = None
        try:
            backup_entity = self._resolve_entity_by_path(project_id, backup_path)
            if not backup_entity.entity_id:
                raise RuntimeError(f"Unable to find backup fileRef ID: {backup_path}")
            self.delete_entity(
                project_id=project_id,
                entity_type="fileRef",
                entity_id=backup_entity.entity_id,
            )
        except Exception as exc:
            delete_backup_error = str(exc)
            logger.warning("Failed to delete backup file %s: %s", backup_path, exc)

        self._invalidate_caches(project_id)
        replaced_entity = self._resolve_entity_by_path(
            project_id=project_id,
            path=f"{parent_path}/{final_name}" if parent_path else f"/{final_name}",
        )
        uploaded_bytes = uploaded_payload.get("bytes") if uploaded_payload else None
        uploaded_bytes = uploaded_bytes if isinstance(uploaded_bytes, int) else None

        return {
            "ok": True,
            "project_id": project_id,
            "old_path": path,
            "new_path": replaced_entity.path,
            "old_entity_id": target.entity_id,
            "new_entity_id": replaced_entity.entity_id,
            "new_hash": replaced_entity.hash,
            "backup_deleted": delete_backup_error is None,
            "backup_delete_error": delete_backup_error,
            "rollback_failed": rollback_failed,
            "uploaded_bytes": uploaded_bytes,
        }

    def write_file(self, project_id: str, path: str, content: str) -> dict[str, str | bool]:
        project_id = validate_project_id(project_id)
        target = self._resolve_entity_by_path(project_id, path)
        if target.type != "doc":
            raise RuntimeError(
                "Only doc-type text files can be written in the current version. "
                "For binary resources, use upload_file or replace_file."
            )
        if not target.entity_id:
            raise RuntimeError(f"Unable to find document ID: {path}")

        current = self.read_file(project_id, path)["content"]
        if current == content:
            return {
                "project_id": project_id,
                "path": path,
                "changed": False,
                "message": "File content unchanged, skipped write",
            }

        try:
            operations = _compute_diff_operations(current, content)
        except Exception:
            logger.exception("Diff computation failed, falling back to full replacement")
            operations = [{"p": 0, "d": current}, {"p": 0, "i": content}]

        logger.info(
            "Writing to %s in project %s (diff: %d ops, was %d chars, now %d chars)",
            path, project_id, len(operations), len(current), len(content),
        )
        if logger.isEnabledFor(logging.DEBUG):
            op_count = len(operations)
            if op_count <= 50:
                logger.debug("Diff operations for %s: %s", path, operations)
            else:
                logger.debug(
                    "Diff operations for %s: %d ops (first 10: %s)",
                    path, op_count, operations[:10],
                )

        self.realtime_client.join_doc_and_apply_ot(
            project_id=project_id,
            doc_id=validate_path_segment(target.entity_id, "doc_id"),
            operations=operations,
        )
        self._invalidate_caches(project_id)
        logger.debug("Write operation sent for %s in project %s (diff: %d ops)", path, project_id, len(operations))
        return {
            "project_id": project_id,
            "path": path,
            "changed": True,
            "message": "Write operation sent",
        }

    def delete_entity(self, project_id: str, entity_type: str, entity_id: str) -> dict[str, str]:
        project_id = validate_project_id(project_id)
        entity_id = validate_entity_id(entity_id)
        mapped_type = self._map_entity_type(entity_type)
        logger.info("Deleting entity %s (%s) from project %s", entity_id, entity_type, project_id)
        result = self._delete_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/{mapped_type}/{entity_id}",
        )
        if result.status_code >= 400:
            raise RuntimeError(f"Failed to delete entity, status code: {result.status_code}")
        self._cache_delete_by_entity_id(project_id, entity_id)
        self._invalidate_caches(project_id)
        return {
            "project_id": project_id,
            "entity_id": entity_id,
            "entity_type": entity_type,
        }

    def rename_entity(self, project_id: str, path: str, new_name: str) -> dict[str, str]:
        project_id = validate_project_id(project_id)
        if not new_name or "/" in new_name:
            raise RuntimeError("new_name is invalid: must not be empty and must not contain slashes")

        target = self._resolve_entity_by_path(project_id, path)
        if not target.entity_id:
            raise RuntimeError(f"Unable to find entity ID: {path}")
        target_entity_id = validate_path_segment(target.entity_id, "entity_id")

        logger.info("Renaming %s → %s in project %s", path, new_name, project_id)
        mapped_type = self._map_entity_type(target.type)
        result = self._post_json_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/{mapped_type}/{target_entity_id}/rename",
            payload={"name": new_name},
            extra_headers={"Accept": "application/json"},
        )
        if result.status_code >= 400:
            raise RuntimeError(f"Failed to rename, status code: {result.status_code}")

        parent_path = target.path.rsplit("/", 1)[0]
        new_path = f"{parent_path}/{new_name}" if parent_path else f"/{new_name}"
        self._cache_delete_by_path(project_id, target.path)
        self._cache_upsert(
            project_id,
            ProjectEntity(
                path=new_path,
                type=target.type,
                entity_id=target.entity_id,
                parent_folder_id=target.parent_folder_id,
                hash=target.hash,
            ),
        )
        self._invalidate_caches(project_id)
        return {
            "project_id": project_id,
            "entity_id": target.entity_id,
            "entity_type": target.type,
            "old_path": target.path,
            "new_path": new_path,
        }

    def move_entity(self, project_id: str, path: str, target_folder_path: str) -> dict[str, str]:
        project_id = validate_project_id(project_id)
        target = self._resolve_entity_by_path(project_id, path)
        if not target.entity_id:
            raise RuntimeError(f"Unable to find entity ID: {path}")
        target_entity_id = validate_path_segment(target.entity_id, "entity_id")

        destination_folder_id, normalized_folder_path = self._resolve_folder_id_by_path(
            project_id=project_id,
            folder_path=target_folder_path,
        )
        logger.info("Moving %s → %s in project %s", path, target_folder_path, project_id)
        mapped_type = self._map_entity_type(target.type)
        result = self._post_json_with_csrf(
            project_id=project_id,
            path=f"/project/{project_id}/{mapped_type}/{target_entity_id}/move",
            payload={"folder_id": destination_folder_id},
            extra_headers={"Accept": "application/json"},
        )
        if result.status_code >= 400:
            raise RuntimeError(f"Failed to move entity, status code: {result.status_code}")

        entity_name = target.path.rsplit("/", 1)[-1]
        new_path = f"{normalized_folder_path}/{entity_name}" if normalized_folder_path else f"/{entity_name}"
        self._cache_delete_by_path(project_id, target.path)
        self._cache_upsert(
            project_id,
            ProjectEntity(
                path=new_path,
                type=target.type,
                entity_id=target.entity_id,
                parent_folder_id=destination_folder_id,
                hash=target.hash,
            ),
        )
        self._invalidate_caches(project_id)
        return {
            "project_id": project_id,
            "entity_id": target.entity_id,
            "entity_type": target.type,
            "old_path": target.path,
            "new_path": new_path,
            "target_folder_path": normalized_folder_path or "/",
        }

    def _collect_entities_from_folder(
        self,
        folder: dict[str, Any],
        parent_path: str,
        parent_folder_id: str | None,
        output: list[ProjectEntity],
    ) -> None:
        if folder.get("name") == "rootFolder":
            folder_path = parent_path
            current_folder_id = folder.get("_id")
        else:
            folder_name = folder.get("name", "")
            if not folder_name:
                folder_path = parent_path
                current_folder_id = folder.get("_id")
            else:
                folder_path = f"{parent_path}/{folder_name}" if parent_path else f"/{folder_name}"
                current_folder_id = folder.get("_id")
                output.append(
                    ProjectEntity(
                        path=folder_path,
                        type="folder",
                        entity_id=current_folder_id,
                        parent_folder_id=parent_folder_id,
                    )
                )

        for doc in folder.get("docs", []):
            doc_name = doc.get("name")
            if not doc_name:
                continue
            doc_path = f"{folder_path}/{doc_name}" if folder_path else f"/{doc_name}"
            output.append(
                ProjectEntity(
                    path=doc_path,
                    type="doc",
                    entity_id=doc.get("_id"),
                    parent_folder_id=current_folder_id,
                )
            )

        for file_ref in folder.get("fileRefs", []):
            file_name = file_ref.get("name")
            if not file_name:
                continue
            file_path = f"{folder_path}/{file_name}" if folder_path else f"/{file_name}"
            output.append(
                ProjectEntity(
                    path=file_path,
                    type="fileRef",
                    entity_id=file_ref.get("_id"),
                    parent_folder_id=current_folder_id,
                    hash=file_ref.get("hash"),
                )
            )

        for child_folder in folder.get("folders", []):
            self._collect_entities_from_folder(
                folder=child_folder,
                parent_path=folder_path,
                parent_folder_id=current_folder_id,
                output=output,
            )

    def _resolve_folder_path(self, project_id: str, folder_id: str) -> str:
        project_id = validate_project_id(project_id)
        folder_id = validate_path_segment(folder_id, "folder_id")
        entities = self.list_files_with_ids(project_id)
        target = next(
            (
                entity
                for entity in entities
                if entity.type == "folder" and entity.entity_id == folder_id
            ),
            None,
        )
        if target is None:
            return ""
        return target.path

    def _resolve_folder_id_by_path(self, project_id: str, folder_path: str) -> tuple[str, str]:
        project_id = validate_project_id(project_id)
        normalized = folder_path.strip() if folder_path else "/"
        if normalized in {"", "/"}:
            project = self.get_project_tree(project_id)
            root_folders = project.get("rootFolder", [])
            if not root_folders or not root_folders[0].get("_id"):
                raise RuntimeError("Unable to determine rootFolder ID")
            return validate_path_segment(root_folders[0]["_id"], "root_folder_id"), ""

        entities = self.list_files_with_ids(project_id)
        target = next(
            (entity for entity in entities if entity.path == normalized and entity.type == "folder"),
            None,
        )
        if target is None or not target.entity_id:
            raise RuntimeError(f"Target folder does not exist in project: {folder_path}")
        return validate_path_segment(target.entity_id, "folder_id"), target.path

    def _fetch_output_file_text(
        self,
        output_file: dict[str, Any],
        compile_payload: dict[str, object],
        max_bytes: int,
    ) -> str:
        file_url = self._resolve_output_file_url(output_file, compile_payload=compile_payload)
        logger.debug("Fetching output file %s (max %d bytes)", file_url, max_bytes)
        result = self.session_manager.http.get_absolute(file_url)
        if result.status_code != 200:
            raise RuntimeError(
                f"Failed to read compile output file, status code: {result.status_code}"
            )
        return result.text[:max_bytes]

    def _resolve_output_file_url(self, output_file: dict[str, Any], compile_payload: dict[str, object]) -> str:
        raw_url = output_file.get("url")
        if not isinstance(raw_url, str) or not raw_url:
            raise RuntimeError("Compile output item missing url, unable to read log")

        parsed_base = urlparse(self.session_manager.config.base_url)
        base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        pdf_download_domain = compile_payload.get("pdfDownloadDomain")
        if isinstance(pdf_download_domain, str) and pdf_download_domain.strip():
            origin = self._normalize_download_domain(pdf_download_domain.strip(), default_scheme=parsed_base.scheme)
        else:
            origin = base_origin

        resolved = raw_url if urlparse(raw_url).scheme else urljoin(origin.rstrip("/") + "/", raw_url.lstrip("/"))

        clsi_server_id = compile_payload.get("clsiServerId") or compile_payload.get("clsiserverid")
        if isinstance(clsi_server_id, str) and clsi_server_id:
            resolved = self._add_query_param_if_missing(resolved, "clsiserverid", clsi_server_id)
        return resolved

    @staticmethod
    def _normalize_download_domain(domain: str, default_scheme: str) -> str:
        if domain.startswith("//"):
            return f"{default_scheme}:{domain}".rstrip("/")
        if not urlparse(domain).scheme:
            return f"{default_scheme}://{domain.lstrip('/')}".rstrip("/")
        return domain.rstrip("/")

    @staticmethod
    def _add_query_param_if_missing(url: str, key: str, value: str) -> str:
        parsed = urlsplit(url)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        if not any(item_key == key for item_key, _ in query_items):
            query_items.append((key, value))
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query_items),
                parsed.fragment,
            )
        )

    @staticmethod
    def _parse_compile_logs(logs_payload: dict[str, Any]) -> list[dict[str, object]]:
        diagnostics: list[dict[str, object]] = []

        output_log = logs_payload.get("output_log")
        if isinstance(output_log, str) and output_log.strip():
            diagnostics.extend(ProjectClient._parse_output_log(output_log, source="output.log"))

        bib_logs = logs_payload.get("bib_logs") or []
        for item in bib_logs:
            path = item.get("path") if isinstance(item, dict) else None
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(path, str) and isinstance(content, str) and content.strip():
                diagnostics.extend(ProjectClient._parse_bib_log(content, source=path))

        return ProjectClient._deduplicate_diagnostics(diagnostics)

    @staticmethod
    def _parse_output_log(content: str, source: str) -> list[dict[str, object]]:
        diagnostics: list[dict[str, object]] = []
        lines = content.splitlines()
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue

            if line.startswith("!"):
                block = [line]
                if index + 1 < len(lines):
                    block.append(lines[index + 1].strip())
                if index + 2 < len(lines) and lines[index + 2].strip().startswith("l."):
                    block.append(lines[index + 2].strip())
                raw = "\n".join(item for item in block if item)
                diagnostics.append(ProjectClient._classify_latex_error_block(raw, source=source))
                index += len(block)
                continue

            warning = ProjectClient._classify_output_log_line(line, source=source)
            if warning is not None:
                diagnostics.append(warning)

            index += 1

        return diagnostics

    @staticmethod
    def _classify_latex_error_block(raw: str, source: str) -> dict[str, object]:
        line_number = _extract_line_number(raw)
        lowered = raw.lower()

        missing_match = re.search(r"! LaTeX Error: File `([^`]+)' not found\.", raw)
        if missing_match:
            message = f"Missing file: {missing_match.group(1)}"
            return ProjectClient._make_diagnostic(
                source=source,
                severity="error",
                kind="missing-file",
                message=message,
                raw=raw,
                line=line_number,
                file=missing_match.group(1),
            )

        if raw.startswith("! Undefined control sequence."):
            return ProjectClient._make_diagnostic(
                source=source,
                severity="error",
                kind="undefined-control-sequence",
                message="Undefined control sequence",
                raw=raw,
                line=line_number,
            )

        package_match = re.search(r"! Package ([^ ]+) Error: (.+)", raw)
        if package_match:
            return ProjectClient._make_diagnostic(
                source=source,
                severity="error",
                kind="package-error",
                message=f"{package_match.group(1)} package error: {package_match.group(2).strip()}",
                raw=raw,
                line=line_number,
                package=package_match.group(1),
            )

        latex_error_match = re.search(r"! LaTeX Error: (.+)", raw)
        if latex_error_match:
            return ProjectClient._make_diagnostic(
                source=source,
                severity="error",
                kind="latex-error",
                message=latex_error_match.group(1).strip(),
                raw=raw,
                line=line_number,
            )

        if "emergency stop" in lowered:
            return ProjectClient._make_diagnostic(
                source=source,
                severity="error",
                kind="latex-error",
                message="Compilation halted by emergency stop",
                raw=raw,
                line=line_number,
            )

        return ProjectClient._make_diagnostic(
            source=source,
            severity="error",
            kind="latex-error",
            message=raw.splitlines()[0].lstrip("! ").strip(),
            raw=raw,
            line=line_number,
        )

    @staticmethod
    def _classify_output_log_line(line: str, source: str) -> dict[str, object] | None:
        citation_match = re.search(r"LaTeX Warning: Citation `([^`]+)' .* line (\d+)", line)
        if citation_match:
            return ProjectClient._make_diagnostic(
                source=source,
                severity="warning",
                kind="citation-warning",
                message=f"Undefined citation key: {citation_match.group(1)}",
                raw=line,
                line=int(citation_match.group(2)),
            )

        reference_match = re.search(r"LaTeX Warning: Reference `([^`]+)' .* line (\d+)", line)
        if reference_match:
            return ProjectClient._make_diagnostic(
                source=source,
                severity="warning",
                kind="reference-warning",
                message=f"Undefined cross-reference: {reference_match.group(1)}",
                raw=line,
                line=int(reference_match.group(2)),
            )

        package_warning_match = re.search(r"Package ([^ ]+) Warning: (.+)", line)
        if package_warning_match:
            return ProjectClient._make_diagnostic(
                source=source,
                severity="warning",
                kind="package-warning",
                message=f"{package_warning_match.group(1)} package warning: {package_warning_match.group(2).strip()}",
                raw=line,
                package=package_warning_match.group(1),
            )

        if line.startswith("Overfull \\") or line.startswith("Underfull \\"):
            line_start, line_end = _extract_line_range(line)
            return ProjectClient._make_diagnostic(
                source=source,
                severity="warning",
                kind="box-warning",
                message=line,
                raw=line,
                line=line_start,
                line_end=line_end,
            )

        if "LaTeX Font Warning:" in line:
            return ProjectClient._make_diagnostic(
                source=source,
                severity="warning",
                kind="font-warning",
                message=line.split("LaTeX Font Warning:", 1)[1].strip(),
                raw=line,
            )

        if "Rerun to get cross-references right." in line:
            return ProjectClient._make_diagnostic(
                source=source,
                severity="info",
                kind="rerun-needed",
                message="Another compilation pass is needed to resolve cross-references",
                raw=line,
            )

        return None

    @staticmethod
    def _parse_bib_log(content: str, source: str) -> list[dict[str, object]]:
        diagnostics: list[dict[str, object]] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("Warning--"):
                diagnostics.append(
                    ProjectClient._make_diagnostic(
                        source=source,
                        severity="warning",
                        kind="bibtex-warning",
                        message=line.replace("Warning--", "", 1).strip(),
                        raw=line,
                    )
                )
                continue

            lowered = line.lower()
            if "i couldn't open database file" in lowered or "error message" in lowered:
                diagnostics.append(
                    ProjectClient._make_diagnostic(
                        source=source,
                        severity="error",
                        kind="bibtex-warning",
                        message=line,
                        raw=line,
                    )
                )

        return diagnostics

    @staticmethod
    def _make_diagnostic(
        source: str,
        severity: str,
        kind: str,
        message: str,
        raw: str,
        line: int | None = None,
        line_end: int | None = None,
        file: str | None = None,
        package: str | None = None,
    ) -> dict[str, object]:
        return {
            "source": source,
            "severity": severity,
            "kind": kind,
            "message": message,
            "line": line,
            "line_end": line_end,
            "file": file,
            "package": package,
            "suggestion": _build_compile_fix_hint(kind, message),
            "raw": raw,
        }

    @staticmethod
    def _deduplicate_diagnostics(diagnostics: list[dict[str, object]]) -> list[dict[str, object]]:
        seen: set[tuple[object, ...]] = set()
        deduplicated: list[dict[str, object]] = []
        for item in diagnostics:
            key = (
                item.get("source"),
                item.get("severity"),
                item.get("kind"),
                item.get("message"),
                item.get("line"),
                item.get("line_end"),
                item.get("file"),
                item.get("package"),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)
        return deduplicated

    def _parse_projects_from_html(self, html: str) -> Iterable[ProjectSummary]:
        soup = BeautifulSoup(html, "html.parser")

        blob_projects = self._parse_projects_from_meta_blob(soup)
        if blob_projects:
            yield from blob_projects
            return

        for anchor in soup.find_all("a", href=True):
            raw_href = anchor.get("href", "")
            if not isinstance(raw_href, str):
                continue
            href = raw_href
            project_id = _extract_project_id(href)
            if not project_id:
                continue

            name = " ".join(anchor.get_text(" ", strip=True).split())
            if not name:
                name = f"project-{project_id}"

            yield ProjectSummary(
                project_id=project_id,
                name=name,
                project_url=urljoin(self.session_manager.config.base_url + "/", href.lstrip("/")),
            )

    def _parse_projects_from_meta_blob(self, soup: BeautifulSoup) -> list[ProjectSummary]:
        meta = soup.find("meta", attrs={"name": "ol-prefetchedProjectsBlob"})
        if not meta:
            return []

        raw_content = meta.get("content")
        if not isinstance(raw_content, str) or not raw_content:
            return []

        try:
            payload = json.loads(html.unescape(raw_content))
        except json.JSONDecodeError:
            return []

        projects = payload.get("projects", [])
        results: list[ProjectSummary] = []
        for item in projects:
            if not isinstance(item, dict):
                continue
            project_id = item.get("id")
            name = item.get("name")
            if not isinstance(project_id, str) or not isinstance(name, str):
                continue
            try:
                project_id = validate_project_id(project_id)
            except RuntimeError:
                continue

            results.append(
                ProjectSummary(
                    project_id=project_id,
                    name=name,
                    project_url=urljoin(
                        self.session_manager.config.base_url + "/",
                        f"project/{project_id}",
                    ),
                    archived=bool(item.get("archived", False)),
                    trashed=bool(item.get("trashed", False)),
                    access_level=item.get("accessLevel"),
                )
            )

        return results

    @staticmethod
    def _read_meta_json(soup: BeautifulSoup, name: str) -> dict[str, Any] | list[Any] | None:
        content = ProjectClient._read_meta_content(soup, name)
        if not content:
            return None
        try:
            return cast(dict[str, Any] | list[Any] | None, json.loads(content))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _read_meta_dict(soup: BeautifulSoup, name: str) -> dict[str, Any]:
        payload = ProjectClient._read_meta_json(soup, name)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _read_meta_list(soup: BeautifulSoup, name: str) -> list[Any]:
        payload = ProjectClient._read_meta_json(soup, name)
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _read_meta_content(soup: BeautifulSoup, name: str) -> str | None:
        meta = soup.find("meta", attrs={"name": name})
        if not meta:
            return None
        content = meta.get("content")
        return content if isinstance(content, str) else None

    @staticmethod
    def _extract_title(html: str) -> str | None:
        match = re.search(r"<title[^>]*>([^<]+)</title>", html, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None
