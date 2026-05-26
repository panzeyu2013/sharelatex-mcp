from sharelatex_mcp.config import load_config
from sharelatex_mcp.session import OverleafSessionManager


def main() -> None:
    config = load_config()
    session_manager = OverleafSessionManager(config)
    session_manager.ensure_logged_in()
    print("登录验证成功")


if __name__ == "__main__":
    main()
