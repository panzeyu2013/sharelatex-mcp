import os

from sharelatex_mcp.config import load_config
from sharelatex_mcp.projects import ProjectClient
from sharelatex_mcp.session import OverleafSessionManager


def main() -> None:
    config = load_config()
    session = OverleafSessionManager(config)
    session.ensure_logged_in()
    project_client = ProjectClient(session)

    preferred_id = os.getenv("OVERLEAF_PROJECT_ID", "").strip() or config.project_id
    if not preferred_id:
        raise RuntimeError(
            "此探针会在真实项目中创建文档。请先设置 OVERLEAF_PROJECT_ID，"
            "或在 ~/.config/sharelatex-mcp/config.json 中设置 project_id。"
        )
    projects = project_client.list_projects()
    project = None
    project = next((item for item in projects if item.project_id == preferred_id), None)
    if project is None:
        raise RuntimeError(f"未找到指定探针项目: {preferred_id}")
    if project.trashed or project.archived:
        raise RuntimeError(f"指定探针项目已归档或在回收站中: {preferred_id}")
    if project is None:
        raise RuntimeError("没有可用于探测 create 接口的项目")

    project_id = project.project_id
    project_tree = project_client.get_project_tree(project_id)
    root_folders = project_tree.get("rootFolder", [])
    if not root_folders or not root_folders[0].get("_id"):
        raise RuntimeError("未能从项目树中解析 rootFolder ID")
    root = root_folders[0]["_id"]
    csrf = session.get_csrf_token()
    name = ".codex-format-probe-ignore.tex"
    base_headers = {
        "Referer": f"{config.base_url}/project/{project_id}",
        "x-csrf-token": csrf,
    }

    modes = []
    r1 = session.http.post_form(
        f"/project/{project_id}/doc",
        {"parent_folder_id": root, "name": name, "_csrf": csrf},
        headers=base_headers,
    )
    modes.append(("form_with_csrf", r1.status_code, r1.text[:200]))

    r2 = session.http.post_json(
        f"/project/{project_id}/doc",
        {"parent_folder_id": root, "name": name},
        headers=base_headers,
    )
    modes.append(("json_with_csrf_header", r2.status_code, r2.text[:200]))

    r3 = session.http.post_json(
        f"/project/{project_id}/doc",
        {"parent_folder_id": root, "name": name, "_csrf": csrf},
        headers=base_headers,
    )
    modes.append(("json_with_csrf_in_body", r3.status_code, r3.text[:200]))

    r4 = session.http.post_form(
        f"/project/{project_id}/doc",
        {"parent_folder_id": root, "name": name},
        headers=base_headers,
    )
    modes.append(("form_header_only", r4.status_code, r4.text[:200]))

    for label, status, body in modes:
        safe_body = body.replace("\n", " ")[:200]
        print("MODE", label, "STATUS", status, "BODY", safe_body)


if __name__ == "__main__":
    main()
