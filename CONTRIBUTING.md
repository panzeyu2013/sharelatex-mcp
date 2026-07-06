# Contributing

## Development Setup

```bash
uv sync --extra dev
```

Run the server once to generate the default config at `~/.config/sharelatex-mcp/config.json`, then edit it with credentials for a self-hosted ShareLaTeX / Overleaf instance that you are allowed to test against.

## Recommended Validation Flow

Run the checks in this order:

```bash
uv run pytest
uv run python scripts/probe_login.py
uv run python scripts/probe_projects.py
OVERLEAF_PROJECT_ID=<project-id> uv run python scripts/test_write_roundtrip.py
OVERLEAF_PROJECT_ID=<project-id> uv run python scripts/test_compile_roundtrip.py
OVERLEAF_PROJECT_ID=<project-id> uv run python scripts/test_mcp_tools.py
```

## Notes

- `scripts/test_mcp_tools.py` performs create, move, rename, upload, replace, and cleanup operations against a real project. Set `TEST_COMPILE=true` when you also want it to run compile checks.
- Scripts that write, compile, or delete remote project content require `OVERLEAF_PROJECT_ID` or `project_id` in `~/.config/sharelatex-mcp/config.json`.
- Please avoid committing local environment files, generated artifacts, or private instance details.
