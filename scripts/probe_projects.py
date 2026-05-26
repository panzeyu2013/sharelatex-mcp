import json

from sharelatex_mcp.config import load_config
from sharelatex_mcp.projects import ProjectClient
from sharelatex_mcp.session import OverleafSessionManager


def main() -> None:
    config = load_config()
    session_manager = OverleafSessionManager(config)
    project_client = ProjectClient(session_manager)
    projects = project_client.list_projects()
    print(
        json.dumps(
            [project.__dict__ for project in projects],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
