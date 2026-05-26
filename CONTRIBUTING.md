# Contributing

## Development Setup

```bash
uv sync --extra dev
cp .env.example .env
```

Fill in the `.env` file with credentials for a self-hosted ShareLaTeX / Overleaf instance that you are allowed to test against.

## Recommended Validation Flow

Run the checks in this order:

```bash
uv run python scripts/probe_login.py
uv run python scripts/probe_projects.py
uv run python scripts/test_write_roundtrip.py
uv run python scripts/test_compile_roundtrip.py
uv run python scripts/test_compile_diagnostics.py
uv run python scripts/test_mcp_tools.py
```

## Notes

- `scripts/test_mcp_tools.py` performs create, move, rename, upload, replace, compile, and cleanup operations against a real project.
- If you want to pin a specific project for local testing, set `OVERLEAF_PROJECT_ID`.
- Please avoid committing local environment files, generated artifacts, or private instance details.
