import asyncio
import base64
import json
import os
import tempfile
import time

from sharelatex_mcp.config import load_config
from sharelatex_mcp.server import create_server


def _normalize_tool_result(result) -> list[dict]:
    blocks = result[0] if isinstance(result, tuple) else result

    normalized = []
    for block in blocks:
        text = getattr(block, "text", None)
        if not text:
            continue
        normalized.append(json.loads(text))
    return normalized


def _configured_project_id() -> str:
    config = load_config()
    project_id = os.getenv("OVERLEAF_PROJECT_ID", "").strip() or config.project_id
    if not project_id:
        raise RuntimeError(
            "此脚本会修改真实项目。请先设置 OVERLEAF_PROJECT_ID，"
            "或在 ~/.config/sharelatex-mcp/config.json 中设置 project_id。"
        )
    return project_id


def _pick_test_project(projects: list[dict], project_id: str) -> dict:
    matched = next(
        (project for project in projects if project.get("project_id") == project_id),
        None,
    )
    if matched is None:
        raise RuntimeError(f"未找到指定测试项目: {project_id}")
    if matched.get("trashed") or matched.get("archived"):
        raise RuntimeError(f"指定测试项目已归档或在回收站中: {project_id}")
    return matched


async def _find_entity_by_path(server, project_id: str, path: str) -> dict | None:
    files_result = await server.call_tool("list_files", {"project_id": project_id})
    entities = _normalize_tool_result(files_result)
    return next((item for item in entities if item["path"] == path), None)


async def main() -> None:
    preferred_project_id = _configured_project_id()
    server = create_server()
    run_compile_checks = os.getenv("TEST_COMPILE", "").strip().lower() == "true"

    tools = await server.list_tools()
    print("已注册工具：")
    print(json.dumps([tool.name for tool in tools], ensure_ascii=False, indent=2))

    projects_result = await server.call_tool("list_projects", {})
    projects = _normalize_tool_result(projects_result)
    print("\nlist_projects 结果：")
    print(json.dumps(projects[:5], ensure_ascii=False, indent=2))

    if not projects:
        print("\n没有可用于测试的项目，跳过 open_project 验证。")
        return

    test_project = _pick_test_project(projects, preferred_project_id)
    project_id = test_project["project_id"]
    print("\n选中的测试项目：")
    print(json.dumps(test_project, ensure_ascii=False, indent=2))

    open_result = await server.call_tool("open_project", {"project_id": project_id})
    open_payloads = _normalize_tool_result(open_result)
    print("\nopen_project 结果：")
    print(json.dumps(open_payloads[0], ensure_ascii=False, indent=2))

    diagnostics_result = await server.call_tool("get_project_diagnostics", {"project_id": project_id})
    diagnostics_payload = _normalize_tool_result(diagnostics_result)[0]
    print("\nget_project_diagnostics 结果：")
    print(json.dumps(diagnostics_payload, ensure_ascii=False, indent=2))

    root_doc_result = await server.call_tool("get_root_doc", {"project_id": project_id})
    root_doc_payload = _normalize_tool_result(root_doc_result)[0]
    print("\nget_root_doc 结果：")
    print(json.dumps(root_doc_payload, ensure_ascii=False, indent=2))

    if run_compile_checks:
        compile_result = await server.call_tool("compile_project", {"project_id": project_id})
        compile_payload = _normalize_tool_result(compile_result)[0]
        print("\ncompile_project 结果：")
        print(json.dumps(compile_payload, ensure_ascii=False, indent=2))

        compile_logs_result = await server.call_tool(
            "get_compile_logs",
            {"project_id": project_id, "compile_result": compile_payload},
        )
        compile_logs_payload = _normalize_tool_result(compile_logs_result)[0]
        print("\nget_compile_logs 结果：")
        print(json.dumps(compile_logs_payload, ensure_ascii=False, indent=2))

        compile_artifacts_result = await server.call_tool(
            "get_compile_artifacts",
            {"project_id": project_id, "compile_result": compile_payload},
        )
        compile_artifacts_payload = _normalize_tool_result(compile_artifacts_result)[0]
        print("\nget_compile_artifacts 结果：")
        print(json.dumps(compile_artifacts_payload, ensure_ascii=False, indent=2))

        compile_analysis_result = await server.call_tool(
            "analyze_compile_errors",
            {"project_id": project_id, "compile_result": compile_payload},
        )
        compile_analysis_payload = _normalize_tool_result(compile_analysis_result)[0]
        print("\nanalyze_compile_errors 结果：")
        print(json.dumps(compile_analysis_payload, ensure_ascii=False, indent=2))
    else:
        print("\n已跳过 compile 相关自测；如需验证编译链路，请显式设置 TEST_COMPILE=true。")

    files_result = await server.call_tool("list_files", {"project_id": project_id})
    file_payloads = _normalize_tool_result(files_result)
    print("\nlist_files 结果：")
    print(json.dumps(file_payloads[:10], ensure_ascii=False, indent=2))

    tex_file = next((item for item in file_payloads if item["type"] == "doc"), None)
    binary_file = next((item for item in file_payloads if item["type"] == "fileRef"), None)
    if tex_file:
        read_result = await server.call_tool(
            "read",
            {"project_id": project_id, "path": tex_file["path"]},
        )
        read_payloads = _normalize_tool_result(read_result)
        print("\nread 结果：")
        snippet = dict(read_payloads[0])
        snippet["content"] = snippet["content"][:600]
        print(json.dumps(snippet, ensure_ascii=False, indent=2))

        write_result = await server.call_tool(
            "write",
            {
                "project_id": project_id,
                "path": tex_file["path"],
                "content": read_payloads[0]["content"],
            },
        )
        write_payloads = _normalize_tool_result(write_result)
        print("\nwrite（无变更自测）结果：")
        print(json.dumps(write_payloads[0], ensure_ascii=False, indent=2))

        fd, temp_doc_download_path = tempfile.mkstemp(
            prefix="codex-sharelatex-doc-",
            suffix=os.path.splitext(tex_file["path"])[1],
        )
        os.close(fd)
        try:
            download_doc_result = await server.call_tool(
                "download_file",
                {
                    "project_id": project_id,
                    "path": tex_file["path"],
                    "output_path": temp_doc_download_path,
                },
            )
            download_doc_payload = _normalize_tool_result(download_doc_result)[0]
            print("\ndownload_file（doc）结果：")
            print(json.dumps(download_doc_payload, ensure_ascii=False, indent=2))
            if not download_doc_payload.get("ok"):
                raise RuntimeError("download_file(doc) 返回失败")
            if not os.path.exists(temp_doc_download_path):
                raise RuntimeError("download_file(doc) 没有写出目标文件")
            if os.path.getsize(temp_doc_download_path) <= 0:
                raise RuntimeError("download_file(doc) 写出的文件大小为 0")
        finally:
            if os.path.exists(temp_doc_download_path):
                os.remove(temp_doc_download_path)

    if binary_file:
        fd, temp_download_path = tempfile.mkstemp(
            prefix="codex-sharelatex-file-",
            suffix=os.path.splitext(binary_file["path"])[1],
        )
        os.close(fd)
        try:
            download_result = await server.call_tool(
                "download_file",
                {
                    "project_id": project_id,
                    "path": binary_file["path"],
                    "output_path": temp_download_path,
                },
            )
            download_payload = _normalize_tool_result(download_result)[0]
            print("\ndownload_file（fileRef）结果：")
            print(json.dumps(download_payload, ensure_ascii=False, indent=2))
            if not download_payload.get("ok"):
                raise RuntimeError("download_file(fileRef) 返回失败")
            if not os.path.exists(temp_download_path):
                raise RuntimeError("download_file(fileRef) 没有写出目标文件")
            if os.path.getsize(temp_download_path) <= 0:
                raise RuntimeError("download_file(fileRef) 写出的文件大小为 0")
        finally:
            if os.path.exists(temp_download_path):
                os.remove(temp_download_path)

    temp_suffix = int(time.time())
    folder_a_name = f".codex-mcp-folder-a-{temp_suffix}"
    folder_a_renamed_name = f".codex-mcp-folder-a-renamed-{temp_suffix}"
    folder_b_name = f".codex-mcp-folder-b-{temp_suffix}"
    doc_name = f".codex-mcp-doc-{temp_suffix}.tex"
    doc_renamed_name = f".codex-mcp-doc-renamed-{temp_suffix}.tex"
    root_doc_name = f".codex-mcp-root-{temp_suffix}.tex"
    upload_name = f"codex-mcp-upload-{temp_suffix}.png"
    upload_renamed_name = f"codex-mcp-upload-renamed-{temp_suffix}.png"

    folder_a_payload = None
    folder_b_payload = None
    doc_payload = None
    uploaded_file_payload = None
    root_doc_temp_payload = None
    temp_root_path = None

    folder_a_result = await server.call_tool(
        "create_folder",
        {"project_id": project_id, "name": folder_a_name},
    )
    folder_a_payload = _normalize_tool_result(folder_a_result)[0]
    print("\ncreate_folder（folder_a）结果：")
    print(json.dumps(folder_a_payload, ensure_ascii=False, indent=2))

    folder_a_renamed_path = f"/{folder_a_renamed_name}"
    folder_b_path = f"/{folder_b_name}"
    moved_doc_path = f"{folder_b_path}/{doc_renamed_name}"
    moved_upload_path = f"{folder_b_path}/{upload_renamed_name}"

    try:
        rename_folder_result = await server.call_tool(
            "rename_entity",
            {
                "project_id": project_id,
                "path": folder_a_payload["path"],
                "new_name": folder_a_renamed_name,
            },
        )
        rename_folder_payload = _normalize_tool_result(rename_folder_result)[0]
        print("\nrename_entity（folder_a）结果：")
        print(json.dumps(rename_folder_payload, ensure_ascii=False, indent=2))

        renamed_folder = await _find_entity_by_path(server, project_id, folder_a_renamed_path)
        if renamed_folder is None:
            raise RuntimeError(f"重命名后的文件夹不存在: {folder_a_renamed_path}")

        folder_b_result = await server.call_tool(
            "create_folder",
            {"project_id": project_id, "name": folder_b_name},
        )
        folder_b_payload = _normalize_tool_result(folder_b_result)[0]
        print("\ncreate_folder（folder_b）结果：")
        print(json.dumps(folder_b_payload, ensure_ascii=False, indent=2))

        upload_image = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQ"
            "VR42mP8/x8AAwMCAO+iY8cAAAAASUVORK5CYII="
        )
        with tempfile.NamedTemporaryFile(
            prefix="codex-sharelatex-upload-",
            suffix=".png",
            delete=False,
        ) as temp_upload_input:
            temp_upload_input.write(base64.b64decode(upload_image))
        try:
            upload_result = await server.call_tool(
                "upload_file",
                {
                    "project_id": project_id,
                    "local_path": temp_upload_input.name,
                    "target_folder_path": folder_a_renamed_path,
                    "new_name": upload_name,
                },
            )
            upload_payload = _normalize_tool_result(upload_result)[0]
            print("\nupload_file 结果：")
            print(json.dumps(upload_payload, ensure_ascii=False, indent=2))
            if not upload_payload.get("ok"):
                raise RuntimeError("upload_file 返回失败")

            uploaded_file_payload = await _find_entity_by_path(
                server,
                project_id,
                f"{folder_a_renamed_path}/{upload_name}",
            )
            if uploaded_file_payload is None:
                raise RuntimeError(f"上传后的文件不存在: {folder_a_renamed_path}/{upload_name}")

            rename_upload_result = await server.call_tool(
                "rename_entity",
                {
                    "project_id": project_id,
                    "path": f"{folder_a_renamed_path}/{upload_name}",
                    "new_name": upload_renamed_name,
                },
            )
            rename_upload_payload = _normalize_tool_result(rename_upload_result)[0]
            print("\nrename_entity（fileRef）结果：")
            print(json.dumps(rename_upload_payload, ensure_ascii=False, indent=2))

            renamed_upload = await _find_entity_by_path(
                server,
                project_id,
                f"{folder_a_renamed_path}/{upload_renamed_name}",
            )
            if renamed_upload is None:
                raise RuntimeError(f"重命名后的上传文件不存在: {folder_a_renamed_path}/{upload_renamed_name}")
            uploaded_file_payload = renamed_upload

            move_upload_result = await server.call_tool(
                "move_entity",
                {
                    "project_id": project_id,
                    "path": f"{folder_a_renamed_path}/{upload_renamed_name}",
                    "target_folder_path": folder_b_path,
                },
            )
            move_upload_payload = _normalize_tool_result(move_upload_result)[0]
            print("\nmove_entity（fileRef -> folder_b）结果：")
            print(json.dumps(move_upload_payload, ensure_ascii=False, indent=2))

            moved_upload = await _find_entity_by_path(server, project_id, moved_upload_path)
            if moved_upload is None:
                raise RuntimeError(f"移动后的上传文件不存在: {moved_upload_path}")
            uploaded_file_payload = moved_upload

            replace_image = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlE"
                "QVR42mNk+M/wHwAEAQH/cetH5QAAAABJRU5ErkJggg=="
            )
            with tempfile.NamedTemporaryFile(
                prefix="codex-sharelatex-replace-",
                suffix=".png",
                delete=False,
            ) as temp_replace_input:
                temp_replace_input.write(base64.b64decode(replace_image))
            try:
                replace_result = await server.call_tool(
                    "replace_file",
                    {
                        "project_id": project_id,
                        "path": moved_upload_path,
                        "local_path": temp_replace_input.name,
                    },
                )
                replace_payload = _normalize_tool_result(replace_result)[0]
                print("\nreplace_file 结果：")
                print(json.dumps(replace_payload, ensure_ascii=False, indent=2))
                if not replace_payload.get("ok"):
                    raise RuntimeError("replace_file 返回失败")

                replaced_upload = await _find_entity_by_path(server, project_id, moved_upload_path)
                if replaced_upload is None:
                    raise RuntimeError(f"替换后的文件不存在: {moved_upload_path}")
                if replaced_upload.get("hash") == moved_upload.get("hash"):
                    raise RuntimeError("replace_file 后 hash 未发生变化")
                uploaded_file_payload = replaced_upload
            finally:
                if os.path.exists(temp_replace_input.name):
                    os.remove(temp_replace_input.name)

            fd, temp_uploaded_download_path = tempfile.mkstemp(prefix="codex-sharelatex-uploaded-", suffix=".png")
            os.close(fd)
            try:
                download_uploaded_result = await server.call_tool(
                    "download_file",
                    {
                        "project_id": project_id,
                        "path": moved_upload_path,
                        "output_path": temp_uploaded_download_path,
                    },
                )
                download_uploaded_payload = _normalize_tool_result(download_uploaded_result)[0]
                print("\ndownload_file（uploaded fileRef）结果：")
                print(json.dumps(download_uploaded_payload, ensure_ascii=False, indent=2))
                if not download_uploaded_payload.get("ok"):
                    raise RuntimeError("download_file(uploaded fileRef) 返回失败")
                if os.path.getsize(temp_uploaded_download_path) <= 0:
                    raise RuntimeError("download_file(uploaded fileRef) 写出的文件大小为 0")
            finally:
                if os.path.exists(temp_uploaded_download_path):
                    os.remove(temp_uploaded_download_path)

            write_probe_content = (
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "Codex write roundtrip check.\n"
                "\\end{document}\n"
            )
            write_probe_name = f".codex-mcp-write-{temp_suffix}.tex"
            write_probe_path = f"{folder_b_path}/{write_probe_name}"
            write_probe_write_result = await server.call_tool(
                "write",
                {
                    "project_id": project_id,
                    "path": write_probe_path,
                    "content": write_probe_content,
                },
            )
            write_probe_write_payload = _normalize_tool_result(write_probe_write_result)[0]
            print("\nwrite（创建+写入）结果：")
            print(json.dumps(write_probe_write_payload, ensure_ascii=False, indent=2))

            write_probe_read_result = await server.call_tool(
                "read",
                {
                    "project_id": project_id,
                    "path": write_probe_path,
                },
            )
            write_probe_read_payload = _normalize_tool_result(write_probe_read_result)[0]
            print("\nread（写入后回读）结果：")
            read_probe_snippet = dict(write_probe_read_payload)
            read_probe_snippet["content"] = write_probe_read_payload["content"][:400]
            print(json.dumps(read_probe_snippet, ensure_ascii=False, indent=2))
            if write_probe_read_payload["content"] != write_probe_content:
                raise RuntimeError("write 真实写入后回读内容不一致")
        finally:
            if os.path.exists(temp_upload_input.name):
                os.remove(temp_upload_input.name)

        doc_path = f"{folder_a_renamed_path}/{doc_name}"
        doc_result = await server.call_tool(
            "write",
            {
                "project_id": project_id,
                "path": doc_path,
                "content": "",
            },
        )
        doc_payload = _normalize_tool_result(doc_result)[0]
        print("\nwrite（创建空文档，folder_a 内）结果：")
        print(json.dumps(doc_payload, ensure_ascii=False, indent=2))

        original_root_path = root_doc_payload.get("root_doc_path")
        if original_root_path:
            temp_root_path = f"{folder_a_renamed_path}/{root_doc_name}"
            root_doc_temp_result = await server.call_tool(
                "write",
                {
                    "project_id": project_id,
                    "path": temp_root_path,
                    "content": "",
                },
            )
            root_doc_temp_payload = _normalize_tool_result(root_doc_temp_result)[0]
            print("\nwrite（创建 root doc temp）结果：")
            print(json.dumps(root_doc_temp_payload, ensure_ascii=False, indent=2))

            temp_root_path = f"{folder_a_renamed_path}/{root_doc_name}"
            set_root_doc_result = await server.call_tool(
                "set_root_doc",
                {
                    "project_id": project_id,
                    "path": temp_root_path,
                },
            )
            set_root_doc_payload = _normalize_tool_result(set_root_doc_result)[0]
            print("\nset_root_doc（临时文档）结果：")
            print(json.dumps(set_root_doc_payload, ensure_ascii=False, indent=2))
            if set_root_doc_payload.get("root_doc_path") != temp_root_path:
                raise RuntimeError("set_root_doc 未成功切换到临时文档")

            restore_root_doc_result = await server.call_tool(
                "set_root_doc",
                {
                    "project_id": project_id,
                    "path": original_root_path,
                },
            )
            restore_root_doc_payload = _normalize_tool_result(restore_root_doc_result)[0]
            print("\nset_root_doc（恢复原 root doc）结果：")
            print(json.dumps(restore_root_doc_payload, ensure_ascii=False, indent=2))
            if restore_root_doc_payload.get("root_doc_path") != original_root_path:
                raise RuntimeError("未能恢复原始 root doc")
        else:
            print("\n原项目没有 root doc，跳过 set_root_doc 切换测试。")

        rename_doc_result = await server.call_tool(
            "rename_entity",
            {
                "project_id": project_id,
                "path": f"{folder_a_renamed_path}/{doc_name}",
                "new_name": doc_renamed_name,
            },
        )
        rename_doc_payload = _normalize_tool_result(rename_doc_result)[0]
        print("\nrename_entity（doc）结果：")
        print(json.dumps(rename_doc_payload, ensure_ascii=False, indent=2))
        if rename_doc_payload.get("new_path") != f"{folder_a_renamed_path}/{doc_renamed_name}":
            raise RuntimeError("rename_entity(doc) 返回的新路径与预期不一致")

        move_doc_result = await server.call_tool(
            "move_entity",
            {
                "project_id": project_id,
                "path": f"{folder_a_renamed_path}/{doc_renamed_name}",
                "target_folder_path": folder_b_path,
            },
        )
        move_doc_payload = _normalize_tool_result(move_doc_result)[0]
        print("\nmove_entity（doc -> folder_b）结果：")
        print(json.dumps(move_doc_payload, ensure_ascii=False, indent=2))
        if move_doc_payload.get("new_path") != moved_doc_path:
            raise RuntimeError("move_entity(doc) 返回的新路径与预期不一致")
    finally:
        root_doc_temp_is_current = False
        cleanup_error = None
        if root_doc_temp_payload is not None:
            current_root_result = await server.call_tool("get_root_doc", {"project_id": project_id})
            current_root_payload = _normalize_tool_result(current_root_result)[0]
            root_doc_temp_is_current = (
                current_root_payload.get("root_doc_id") == root_doc_temp_payload["entity_id"]
                or current_root_payload.get("root_doc_path") == temp_root_path
            )
            if root_doc_temp_is_current:
                cleanup_error = (
                    "临时 root doc 仍是当前 root doc，已跳过删除临时 root doc 和其父文件夹，"
                    "请先恢复项目 root doc 后再清理。"
                )

        if doc_payload is not None:
            delete_doc_result = await server.call_tool(
                "delete_entity",
                {
                    "project_id": project_id,
                    "entity_type": "doc",
                    "entity_id": doc_payload["entity_id"],
                },
            )
            delete_doc_payload = _normalize_tool_result(delete_doc_result)[0]
            print("\ndelete_entity（doc）结果：")
            print(json.dumps(delete_doc_payload, ensure_ascii=False, indent=2))

        if root_doc_temp_payload is not None and not root_doc_temp_is_current:
            delete_root_doc_result = await server.call_tool(
                "delete_entity",
                {
                    "project_id": project_id,
                    "entity_type": "doc",
                    "entity_id": root_doc_temp_payload["entity_id"],
                },
            )
            delete_root_doc_payload = _normalize_tool_result(delete_root_doc_result)[0]
            print("\ndelete_entity（root doc temp）结果：")
            print(json.dumps(delete_root_doc_payload, ensure_ascii=False, indent=2))
        elif root_doc_temp_payload is not None:
            print(f"\n跳过删除当前 root doc 临时文档: {temp_root_path}")

        if uploaded_file_payload is not None:
            delete_upload_result = await server.call_tool(
                "delete_entity",
                {
                    "project_id": project_id,
                    "entity_type": "fileRef",
                    "entity_id": uploaded_file_payload["entity_id"],
                },
            )
            delete_upload_payload = _normalize_tool_result(delete_upload_result)[0]
            print("\ndelete_entity（uploaded fileRef）结果：")
            print(json.dumps(delete_upload_payload, ensure_ascii=False, indent=2))

        if folder_b_payload is not None:
            delete_folder_b_result = await server.call_tool(
                "delete_entity",
                {
                    "project_id": project_id,
                    "entity_type": "folder",
                    "entity_id": folder_b_payload["entity_id"],
                },
            )
            delete_folder_b_payload = _normalize_tool_result(delete_folder_b_result)[0]
            print("\ndelete_entity（folder_b）结果：")
            print(json.dumps(delete_folder_b_payload, ensure_ascii=False, indent=2))

        if folder_a_payload is not None and not root_doc_temp_is_current:
            delete_folder_a_result = await server.call_tool(
                "delete_entity",
                {
                    "project_id": project_id,
                    "entity_type": "folder",
                    "entity_id": folder_a_payload["entity_id"],
                },
            )
            delete_folder_a_payload = _normalize_tool_result(delete_folder_a_result)[0]
            print("\ndelete_entity（folder_a）结果：")
            print(json.dumps(delete_folder_a_payload, ensure_ascii=False, indent=2))
        elif folder_a_payload is not None:
            print(f"\n跳过删除包含当前 root doc 的临时文件夹: {folder_a_renamed_path}")

        if cleanup_error:
            raise RuntimeError(cleanup_error)


if __name__ == "__main__":
    asyncio.run(main())
