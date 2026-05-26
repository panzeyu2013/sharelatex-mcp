import asyncio
import json
import os
import tempfile
import time

from sharelatex_mcp.server import create_server


def _normalize_tool_result(result) -> list[dict]:
    if isinstance(result, tuple):
        blocks = result[0]
    else:
        blocks = result

    normalized = []
    for block in blocks:
        text = getattr(block, "text", None)
        if not text:
            continue
        normalized.append(json.loads(text))
    return normalized


def _choose_project(projects: list[dict]) -> dict:
    for project in projects:
        if not project.get("trashed") and not project.get("archived"):
            return project
    for project in projects:
        if not project.get("trashed"):
            return project
    if not projects:
        raise RuntimeError("没有可用于编译测试的项目")
    return projects[0]


async def main() -> None:
    server = create_server()

    projects_result = await server.call_tool("list_projects", {})
    projects = _normalize_tool_result(projects_result)
    project = _choose_project(projects)
    project_id = project["project_id"]
    print("selected_project:")
    print(json.dumps(project, ensure_ascii=False, indent=2))

    stop_result = await server.call_tool("stop_compile", {"project_id": project_id})
    print("\nstop_compile:")
    print(json.dumps(_normalize_tool_result(stop_result)[0], ensure_ascii=False, indent=2))

    compile_payload = None
    retry_delays = [0, 20, 40]
    for index, delay in enumerate(retry_delays, start=1):
        if delay:
            time.sleep(delay)
        compile_result = await server.call_tool(
            "compile_project",
            {"project_id": project_id, "force": True},
        )
        compile_payload = _normalize_tool_result(compile_result)[0]
        print(f"\ncompile_project attempt {index}:")
        print(json.dumps(compile_payload, ensure_ascii=False, indent=2))
        if compile_payload.get("status") == "success" and compile_payload.get("outputFiles"):
            break
    else:
        raise RuntimeError("多次尝试后仍未获得 success 编译结果")

    compile_logs_result = await server.call_tool(
        "get_compile_logs",
        {"project_id": project_id, "compile_result": compile_payload},
    )
    compile_logs_payload = _normalize_tool_result(compile_logs_result)[0]
    compile_logs_preview = dict(compile_logs_payload)
    output_log = compile_logs_preview.get("output_log")
    if isinstance(output_log, str):
        compile_logs_preview["output_log"] = output_log[:2000]
    print("\nget_compile_logs:")
    print(json.dumps(compile_logs_preview, ensure_ascii=False, indent=2))
    if not compile_logs_payload.get("ok"):
        raise RuntimeError("编译成功后仍未能读取 compile logs")
    if not compile_logs_payload.get("output_log"):
        raise RuntimeError("编译成功后 output.log 为空")

    compile_artifacts_result = await server.call_tool(
        "get_compile_artifacts",
        {"project_id": project_id, "compile_result": compile_payload},
    )
    compile_artifacts_payload = _normalize_tool_result(compile_artifacts_result)[0]
    print("\nget_compile_artifacts:")
    print(json.dumps(compile_artifacts_payload, ensure_ascii=False, indent=2))
    if not compile_artifacts_payload.get("ok"):
        raise RuntimeError("编译成功后仍未能列出 compile artifacts")
    if not compile_artifacts_payload.get("pdf"):
        raise RuntimeError("编译成功后产物中没有 output.pdf")

    fd, temp_pdf_path = tempfile.mkstemp(prefix="codex-sharelatex-", suffix=".pdf")
    os.close(fd)
    try:
        download_pdf_result = await server.call_tool(
            "download_pdf",
            {
                "project_id": project_id,
                "compile_result": compile_payload,
                "output_path": temp_pdf_path,
            },
        )
        download_pdf_payload = _normalize_tool_result(download_pdf_result)[0]
        print("\ndownload_pdf:")
        print(json.dumps(download_pdf_payload, ensure_ascii=False, indent=2))
        if not download_pdf_payload.get("ok"):
            raise RuntimeError("download_pdf 返回失败")
        if not os.path.exists(temp_pdf_path):
            raise RuntimeError("download_pdf 没有写出目标 PDF 文件")
        if os.path.getsize(temp_pdf_path) <= 0:
            raise RuntimeError("download_pdf 写出的 PDF 文件大小为 0")
    finally:
        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)

    print("\nsummary:")
    print(
        json.dumps(
            {
                "project_id": project_id,
                "compile_status": compile_payload.get("status"),
                "output_files_count": len(compile_payload.get("outputFiles") or []),
                "artifact_count": len(compile_artifacts_payload.get("artifacts") or []),
                "pdf_bytes": download_pdf_payload.get("bytes"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
