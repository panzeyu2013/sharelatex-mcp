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


async def main() -> None:
    filename = f".codex-mcp-write-test-{int(time.time())}.tex"
    content = "\\documentclass{article}\n\\begin{document}\nsharelatex-mcp write test.\n\\end{document}\n"

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

    created = await server.call_tool("create_doc", {"project_id": project_id, "name": filename})
    created_payload = _normalize_tool_result(created)[0]
    path = created_payload["path"]
    print("created:")
    print(json.dumps(created_payload, ensure_ascii=False, indent=2))

    try:
        write_result = await server.call_tool(
            "write_file",
            {"project_id": project_id, "path": path, "content": content},
        )
        print("\nwrite:")
        print(json.dumps(_normalize_tool_result(write_result)[0], ensure_ascii=False, indent=2))

        read_result = await server.call_tool(
            "read_file",
            {"project_id": project_id, "path": path},
        )
        read_payload = _normalize_tool_result(read_result)[0]
        print("\nread:")
        print(json.dumps(read_payload, ensure_ascii=False, indent=2))

        if read_payload["content"] != content:
            raise RuntimeError("写回后读取内容不一致")
    finally:
        entities = project_client.list_files_with_ids(project_id)
        target = next((entity for entity in entities if entity.path == path), None)
        if target and target.entity_id:
            deleted = project_client.delete_entity(project_id, target.type, target.entity_id)
            print("\ndeleted:")
            print(json.dumps(deleted, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
