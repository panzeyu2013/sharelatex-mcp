from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict

import anyio
from mcp.server.fastmcp import Context, FastMCP

from sharelatex_mcp.config import AppConfig, load_config
from sharelatex_mcp.diff_engine import MAX_FILE_SIZE
from sharelatex_mcp.doc_editor import DocEditor
from sharelatex_mcp.errors import FileSizeError
from sharelatex_mcp.jobs import JobStore, ProgressCallback
from sharelatex_mcp.projects import ProjectClient
from sharelatex_mcp.session import OverleafSessionManager

logger = logging.getLogger(__name__)


def create_server(
    config: AppConfig | None = None,
    project_client: ProjectClient | None = None,
    job_store: JobStore | None = None,
) -> FastMCP:
    """Build the MCP server.

    Optional *config* / *project_client* / *job_store* overrides are for tests.
    """
    config = config if config is not None else load_config()
    session_manager = OverleafSessionManager(config)
    project_client = project_client if project_client is not None else ProjectClient(session_manager)
    doc_editor = DocEditor(project_client)
    job_store = job_store if job_store is not None else JobStore()

    def _submit_job(operation: str, project_id: str, fn) -> dict:
        job_id = job_store.submit(operation, project_id, fn)
        return {
            "job_id": job_id,
            "operation": operation,
            "project_id": project_id,
            "status": "queued",
            "async": True,
            "message": (
                "Running in the background. Poll get_job_status or block "
                "with wait_job to retrieve the result."
            ),
        }

    def _wire_progress(ctx: Context) -> ProgressCallback:
        """Return a progress callback that forwards to MCP on the event loop.

        Called from ``anyio.to_thread.run_sync`` worker threads, which are
        anyio-managed, so ``anyio.from_thread.run`` can schedule the async
        ``report_progress`` onto the server loop without a cross-thread token.
        """
        warned = False

        def report(done: int, total: int, message: str | None) -> None:
            nonlocal warned
            try:
                anyio.from_thread.run(ctx.report_progress, done, total, message)
            except Exception:
                if not warned:
                    warned = True
                    logger.warning("Progress notification failed; progress will be silent", exc_info=True)
                else:
                    logger.debug("Progress notification failed", exc_info=True)

        return report

    mcp = FastMCP(
        name="sharelatex-mcp",
        instructions=(
            "MCP server for self-hosted ShareLaTeX/Overleaf. "
            "Supports email login, project CRUD, file upload/download/replace, "
            "doc text read/write/edit, and compile workflows."
        ),
        log_level=config.log_level,
    )

    # ==================================================================
    # Project management
    # ==================================================================

    @mcp.tool(
        name="list_projects",
        description=(
            "List accessible projects. Returns project_id, name, URL, and archive/trash status."
        ),
    )
    def list_projects() -> list[dict]:
        projects = project_client.list_projects()
        return [asdict(project) for project in projects]

    @mcp.tool(
        name="open_project",
        description=(
            "Open a project by id. Returns status code, title, and HTML snippet."
        ),
    )
    def open_project(project_id: str) -> dict:
        return project_client.open_project(project_id)

    @mcp.tool(
        name="get_project_diagnostics",
        description=(
            "Read Overleaf metadata from a project page. Returns compile settings, "
            "feature flags, editor config, and other diagnostics."
        ),
    )
    def get_project_diagnostics(project_id: str) -> dict:
        return project_client.get_project_diagnostics(project_id)

    @mcp.tool(
        name="get_root_doc",
        description=(
            "Get the current root compilation document. Returns root_doc_id and path."
        ),
    )
    def get_root_doc(project_id: str) -> dict:
        return project_client.get_root_doc(project_id)

    @mcp.tool(
        name="set_root_doc",
        description=(
            "Set a doc file as the project root document. "
            "Useful for switching compilation entry points in multi-doc projects."
        ),
    )
    def set_root_doc(project_id: str, path: str) -> dict:
        return project_client.set_root_doc(project_id, path)

    # ==================================================================
    # File listing & reading
    # ==================================================================

    @mcp.tool(
        name="list_files",
        description=(
            "List all file entities in a project. Returns a flat list with path "
            "and type for each entity (including folders, docs, and fileRefs)."
        ),
    )
    def list_files(project_id: str) -> list[dict]:
        entities = project_client.list_files_with_ids(project_id)
        return [asdict(entity) for entity in entities]

    @mcp.tool(
        name="read",
        description=(
            "Read text content of a doc-type file (.tex, .bib, .sty, etc.) "
            "with optional line-range slicing. Returns line-numbered content. "
            "Use offset/limit for large files. "
            "Path must start with / and match the project-internal path. "
            "For binary files use download_file. "
            "Note: offset/limit slicing is applied after the full document is "
            "transferred, so very large files are still costly to read; prefer "
            "'edit' for targeted changes."
        ),
    )
    def read(
        project_id: str,
        path: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict:
        return doc_editor.read(project_id, path, offset=offset, limit=limit)


    @mcp.tool(
        name="download_file",
        description=(
            "Download a file by project path to a local path. "
            "Supports doc and fileRef. Defaults to downloads/<project_id>/..."
        ),
    )
    def download_file(project_id: str, path: str, output_path: str | None = None) -> dict:
        return project_client.download_file(project_id, path, output_path)

    # ==================================================================
    # File upload & replace
    # ==================================================================

    @mcp.tool(
        name="upload_file",
        description=(
            "Upload a local file to a project folder. Uses the ShareLaTeX upload "
            "endpoint. Suitable for images, PDFs, and other fileRef resources."
        ),
    )
    def upload_file(
        project_id: str,
        local_path: str,
        target_folder_path: str = "/",
        new_name: str | None = None,
    ) -> dict:
        return project_client.upload_file(
            project_id=project_id,
            local_path=local_path,
            target_folder_path=target_folder_path,
            new_name=new_name,
        )

    @mcp.tool(
        name="replace_file",
        description=(
            "Replace an existing fileRef with a local file. "
            "Keeps the original filename unless new_name is specified. "
            "Uses a temporary backup → upload → delete-backup flow."
        ),
    )
    def replace_file(
        project_id: str,
        path: str,
        local_path: str,
        new_name: str | None = None,
    ) -> dict:
        return project_client.replace_file(
            project_id=project_id,
            path=path,
            local_path=local_path,
            new_name=new_name,
        )

    # ==================================================================
    # Write & edit
    # ==================================================================

    @mcp.tool(
        name="write",
        description=(
            "Write content to a doc-type text file. Auto-creates the file if "
            "it does not already exist. Uses socket.io + sharejs-text-ot to "
            "apply minimal character-level diffs. For binary files use "
            "upload_file or replace_file. "
            "Very large content is automatically run in the background "
            "(async_mode=true) to avoid client request timeouts; the tool then "
            "returns a job_id to poll with get_job_status or wait_job. Pass "
            "async_mode=false to force synchronous execution."
        ),
    )
    async def write(
        project_id: str,
        path: str,
        content: str,
        async_mode: bool | None = None,
        ctx: Context | None = None,
    ) -> dict:
        content_bytes = len(content.encode("utf-8"))
        # Validate the size before routing so oversized input fails fast with a
        # clear error instead of being queued (and holding the content in memory).
        if content_bytes > MAX_FILE_SIZE:
            raise FileSizeError(f"Content exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit")
        if async_mode is None:
            async_mode = content_bytes > config.async_write_threshold_bytes
        if async_mode:
            return _submit_job(
                "write", project_id, lambda: doc_editor.write(project_id, path, content)
            )
        if ctx is None:
            return await anyio.to_thread.run_sync(doc_editor.write, project_id, path, content)
        progress = _wire_progress(ctx)
        return await anyio.to_thread.run_sync(
            doc_editor.write, project_id, path, content, progress
        )

    @mcp.tool(
        name="edit",
        description=(
            "Apply precise find-and-replace edits to a doc-type text file. "
            "Each edit has 'old' (text to find, must match exactly one location) "
            "and 'new' (replacement text). Multiple edits are applied atomically "
            "in a single operation. For full-file writes use 'write' instead. "
            "Large edit batches are automatically run in the background "
            "(async_mode=true); the tool then returns a job_id to poll with "
            "get_job_status or wait_job. Pass async_mode=false to force "
            "synchronous execution."
        ),
    )
    async def edit(
        project_id: str,
        path: str,
        edits: list[dict[str, str]],
        async_mode: bool | None = None,
        ctx: Context | None = None,
    ) -> dict:
        # Validate the batch before routing so invalid edits fail fast with a
        # clear error instead of being queued and failing in the background.
        doc_editor._validate_edits(edits)
        edit_bytes = sum(
            len(e["old"].encode("utf-8")) + len(e["new"].encode("utf-8"))
            for e in edits
        )
        if async_mode is None:
            async_mode = edit_bytes > config.async_write_threshold_bytes
        if async_mode:
            return _submit_job(
                "edit", project_id, lambda: doc_editor.edit(project_id, path, edits)
            )
        if ctx is None:
            return await anyio.to_thread.run_sync(doc_editor.edit, project_id, path, edits)
        progress = _wire_progress(ctx)
        return await anyio.to_thread.run_sync(
            doc_editor.edit, project_id, path, edits, progress
        )

    @mcp.tool(
        name="get_job_status",
        description=(
            "Get the status and result of a background job returned by write/edit "
            "when running in async mode. Returns status (queued/running/succeeded/"
            "failed), the result payload on success, or an error message on failure."
        ),
    )
    def get_job_status(job_id: str) -> dict:
        snapshot = job_store.status(job_id)
        if snapshot is None:
            return {"job_id": job_id, "status": "not-found"}
        return snapshot

    @mcp.tool(
        name="wait_job",
        description=(
            "Block until a background job finishes (or the timeout elapses), "
            "returning its status and result. For long waits prefer get_job_status "
            "polling so the client request is not held open."
        ),
    )
    async def wait_job(job_id: str, timeout_seconds: float = 30.0) -> dict:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 <= timeout_seconds <= 300
        ):
            raise RuntimeError("timeout_seconds must be between 0 and 300")
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            snapshot = job_store.status(job_id)
            if snapshot is None:
                return {"job_id": job_id, "status": "not-found"}
            if snapshot["status"] in {"succeeded", "failed"}:
                return snapshot
            if time.monotonic() >= deadline:
                snapshot = dict(snapshot)
                snapshot["timed_out"] = True
                return snapshot
            await asyncio.sleep(0.05)

    # ==================================================================
    # File CRUD
    # ==================================================================

    @mcp.tool(
        name="create_folder",
        description=(
            "Create a new folder in a project. "
            "Defaults to root; use parent_folder_id to specify a subfolder."
        ),
    )
    def create_folder(project_id: str, name: str, parent_folder_id: str | None = None) -> dict:
        return project_client.create_folder(project_id, name, parent_folder_id)

    @mcp.tool(
        name="delete_entity",
        description=(
            "Delete an entity (doc, folder, or fileRef) by entity_id. "
            "Use list_files first to confirm the entity_id."
        ),
    )
    def delete_entity(project_id: str, entity_type: str, entity_id: str) -> dict:
        return project_client.delete_entity(project_id, entity_type, entity_id)

    @mcp.tool(
        name="rename_entity",
        description=(
            "Rename an entity by its project path. "
            "Supports doc, folder, and fileRef. new_name is the filename only, not a full path."
        ),
    )
    def rename_entity(project_id: str, path: str, new_name: str) -> dict:
        return project_client.rename_entity(project_id, path, new_name)

    @mcp.tool(
        name="move_entity",
        description=(
            "Move an entity to a target folder. "
            "target_folder_path must start with / (use / for root)."
        ),
    )
    def move_entity(project_id: str, path: str, target_folder_path: str) -> dict:
        return project_client.move_entity(project_id, path, target_folder_path)

    # ==================================================================
    # Compile chain
    # ==================================================================

    @mcp.tool(
        name="compile_project",
        description=(
            "Trigger compilation for a project. Uses the current root doc by default. "
            "Returns compile status and outputFiles metadata."
        ),
    )
    def compile_project(
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
    ) -> dict:
        return project_client.compile_project(
            project_id=project_id,
            root_doc_id=root_doc_id,
            draft=draft,
            stop_on_first_error=stop_on_first_error,
            check=check,
            retry_on_500=retry_on_500,
            retry_delay_seconds=retry_delay_seconds,
            min_interval_seconds=min_interval_seconds,
            force=force,
            allow_compat_variants=allow_compat_variants,
            return_attempt_trace=return_attempt_trace,
        )

    @mcp.tool(
        name="stop_compile",
        description=(
            "Request to stop an ongoing compilation. "
            "Returns structured status even if no compilation is running."
        ),
    )
    def stop_compile(project_id: str) -> dict:
        return project_client.stop_compile(project_id)

    @mcp.tool(
        name="clear_compile_output",
        description="Clear cached compile artifacts for a project.",
    )
    def clear_compile_output(project_id: str) -> dict:
        return project_client.clear_compile_output(project_id)

    @mcp.tool(
        name="get_compile_logs",
        description=(
            "Read the output.log and .blg files from the most recent compilation. "
            "Does not trigger a new compile by default; call compile_project first "
            "and pass its result from the current server process."
        ),
    )
    def get_compile_logs(
        project_id: str,
        compile_result: dict | None = None,
        max_bytes: int = 200000,
        trigger_compile_if_missing: bool = False,
    ) -> dict:
        return project_client.get_compile_logs(
            project_id=project_id,
            compile_result=compile_result,
            max_bytes=max_bytes,
            trigger_compile_if_missing=trigger_compile_if_missing,
        )

    @mcp.tool(
        name="analyze_compile_errors",
        description=(
            "Analyze compile logs for structured diagnostics. "
            "Extracts LaTeX errors, citation/reference warnings, "
            "BibTeX warnings, typesetting warnings, and fix suggestions. "
            "Any compile_result must come from the current server process."
        ),
    )
    def analyze_compile_errors(
        project_id: str,
        compile_result: dict | None = None,
        max_bytes: int = 200000,
        trigger_compile_if_missing: bool = False,
    ) -> dict:
        return project_client.analyze_compile_errors(
            project_id=project_id,
            compile_result=compile_result,
            max_bytes=max_bytes,
            trigger_compile_if_missing=trigger_compile_if_missing,
        )

    @mcp.tool(
        name="get_compile_artifacts",
        description=(
            "List artifacts from the most recent compilation, "
            "including the resolved download URL for output.pdf when available. "
            "Any compile_result must come from the current server process."
        ),
    )
    def get_compile_artifacts(
        project_id: str,
        compile_result: dict | None = None,
        trigger_compile_if_missing: bool = False,
    ) -> dict:
        return project_client.get_compile_artifacts(
            project_id=project_id,
            compile_result=compile_result,
            trigger_compile_if_missing=trigger_compile_if_missing,
        )

    @mcp.tool(
        name="download_pdf",
        description=(
            "Download the output.pdf from the most recent compilation. "
            "Pass a compile_result from the current server process to avoid "
            "triggering a new compile."
        ),
    )
    def download_pdf(
        project_id: str,
        compile_result: dict | None = None,
        output_path: str | None = None,
        trigger_compile_if_missing: bool = False,
    ) -> dict:
        return project_client.download_pdf(
            project_id=project_id,
            compile_result=compile_result,
            output_path=output_path,
            trigger_compile_if_missing=trigger_compile_if_missing,
        )

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
