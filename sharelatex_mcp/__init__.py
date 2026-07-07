"""ShareLaTeX MCP package."""

from sharelatex_mcp.config import AppConfig, load_config
from sharelatex_mcp.diff_engine import compute_diff_operations
from sharelatex_mcp.doc_editor import DocEditor
from sharelatex_mcp.http import BinaryHttpResult, HttpClient, HttpResult
from sharelatex_mcp.projects import ProjectClient, ProjectEntity, ProjectSummary
from sharelatex_mcp.realtime import RealtimeProjectClient
from sharelatex_mcp.server import create_server
from sharelatex_mcp.session import OverleafSessionManager

__version__ = "0.1.0"

__all__ = [
    "AppConfig",
    "BinaryHttpResult",
    "DocEditor",
    "HttpClient",
    "HttpResult",
    "OverleafSessionManager",
    "ProjectClient",
    "ProjectEntity",
    "ProjectSummary",
    "RealtimeProjectClient",
    "compute_diff_operations",
    "create_server",
    "load_config",
]
