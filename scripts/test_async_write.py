"""End-to-end validation of the background (async) write workflow.

Creates a large temporary doc in a real project via ``write async_mode=true``,
polls ``get_job_status`` until it finishes, verifies the content with ``read``,
then deletes the doc.

Requires ``OVERLEAF_PROJECT_ID`` or a ``project_id`` in the config, and refuses
to run without one (it modifies a real project).
"""

import asyncio
import json
import os
import time

from sharelatex_mcp.config import load_config
from sharelatex_mcp.projects import ProjectClient
from sharelatex_mcp.server import create_server
from sharelatex_mcp.session import OverleafSessionManager


def _normalize_tool_result(result):
    blocks = result[0] if isinstance(result, tuple) else result

    normalized = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            normalized.append(json.loads(text))
    return normalized


def _configured_project_id(config) -> str:
    project_id = os.getenv("OVERLEAF_PROJECT_ID", "").strip() or config.project_id
    if not project_id:
        raise RuntimeError(
            "此脚本会创建、写入并删除真实项目文档。请先设置 OVERLEAF_PROJECT_ID，"
            "或在 ~/.config/sharelatex-mcp/config.json 中设置 project_id。"
        )
    return project_id


def _choose_project(projects: list[dict], project_id: str) -> dict:
    matched = next((project for project in projects if project.get("project_id") == project_id), None)
    if matched is None:
        raise RuntimeError(f"未找到指定写入测试项目: {project_id}")
    if matched.get("trashed") or matched.get("archived"):
        raise RuntimeError(f"指定写入测试项目已归档或在回收站中: {project_id}")
    return matched


async def _wait_for_job(server, job_id: str, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = await server.call_tool(
            "get_job_status",
            {"job_id": job_id},
        )
        payload = _normalize_tool_result(result)[0]
        if payload["status"] in {"succeeded", "failed", "not-found"}:
            return payload
        await asyncio.sleep(0.2)
    raise RuntimeError(f"job {job_id} 未在 {timeout_seconds:.0f}s 内完成")


async def main() -> None:
    filename = f".codex-mcp-async-write-test-{int(time.time())}.tex"
    content = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        + ("sharelatex-mcp async write test.\n" * 20000)
        + "\\end{document}\n"
    )

    server = create_server()
    config = load_config()
    preferred_project_id = _configured_project_id(config)
    session_manager = OverleafSessionManager(config)
    project_client = ProjectClient(session_manager)
    projects_result = await server.call_tool("list_projects", {})
    projects = _normalize_tool_result(projects_result)
    project = _choose_project(projects, preferred_project_id)
    project_id = project["project_id"]
    print("selected_project:")
    print(json.dumps(project, ensure_ascii=False, indent=2))

    path = f"/{filename}"

    try:
        write_result = await server.call_tool(
            "write",
            {
                "project_id": project_id,
                "path": path,
                "content": content,
                "async_mode": True,
            },
        )
        queued = _normalize_tool_result(write_result)[0]
        print("\nwrite（async_mode=true）:")
        print(json.dumps(queued, ensure_ascii=False, indent=2))
        assert queued["async"] is True, "write 未进入异步模式"

        job_id = queued["job_id"]
        job_payload = await _wait_for_job(server, job_id, timeout_seconds=120)
        print("\nget_job_status（轮询结果）:")
        print(json.dumps(job_payload, ensure_ascii=False, indent=2))
        if job_payload["status"] != "succeeded":
            raise RuntimeError(f"异步写入失败: {job_payload.get('error')}")

        read_result = await server.call_tool(
            "read",
            {"project_id": project_id, "path": path},
        )
        read_payload = _normalize_tool_result(read_result)[0]
        print("\nread（回读校验）:")
        print(json.dumps(read_payload, ensure_ascii=False, indent=2))

        if read_payload["content"] != content:
            raise RuntimeError("异步写回后读取内容不一致")
        print("\n异步写入验证通过 ✔")
    finally:
        entities = project_client.list_files_with_ids(project_id)
        target = next((entity for entity in entities if entity.path == path), None)
        if target and target.entity_id:
            deleted = project_client.delete_entity(project_id, target.type, target.entity_id)
            print("\ndeleted:")
            print(json.dumps(deleted, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
