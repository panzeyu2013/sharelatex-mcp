# sharelatex-mcp

> 🚀 A Python MCP server for self-hosted ShareLaTeX / Overleaf instances.  
> Direct web-session access. Dynamic project discovery. No Git bridge required.

<p align="left">
  <a href="./README_CN.md">🇨🇳 中文说明</a>
</p>

## ✨ What Is This?

`sharelatex-mcp` connects MCP directly to a live self-hosted ShareLaTeX / Overleaf service.

Instead of treating Overleaf as a Git remote, this project logs in like a normal web user and talks to:

- the live web session
- the project HTTP endpoints
- the legacy realtime editing channel

That makes it useful when:

- your instance is self-hosted
- Git bridge is unavailable, disabled, or inconvenient
- you want dynamic project discovery after login
- you want to edit the live online project, not a detached local clone

## 🧭 Why It Exists

Many existing Overleaf integrations are really Git workflows with an MCP wrapper around them.

That approach is valid, but it solves a different problem.

This repository is built for teams who want MCP to operate on the actual self-hosted ShareLaTeX / Overleaf service itself.

## ⚡ Key Difference vs Git-Based Overleaf MCPs

| Capability | `sharelatex-mcp` | Typical Git-based Overleaf MCP |
| --- | --- | --- |
| Access model | Direct web session | Local Git sync |
| Requires Git bridge | No | Usually yes |
| Requires fixed project mapping | No | Often yes |
| Dynamic project discovery | Yes | Often limited |
| Live doc editing | Yes | Indirect |
| Live compile control | Yes | Usually no |
| Binary asset upload/download | Yes | Via Git only |
| Self-hosted ShareLaTeX focus | Yes | Not always |

## 🛠️ Current Capabilities

### Project access

- `list_projects`
- `open_project`
- `get_project_diagnostics`
- `get_root_doc`
- `set_root_doc`
- `list_files`

### Text workflows

- `read_file`
- `write_file`
- `create_doc`
- `create_folder`
- `rename_entity`
- `move_entity`
- `delete_entity`

### Binary asset workflows

- `download_file`
- `upload_file`
- `replace_file`

### Compile workflows

- `compile_project`
- `stop_compile`
- `clear_compile_output`
- `get_compile_logs`
- `analyze_compile_errors`
- `get_compile_artifacts`
- `download_pdf`

## ✅ Verified on a Real Self-Hosted Instance

The following flows were validated against a live ShareLaTeX-derived deployment:

- email/password login
- dynamic project discovery
- text doc read/write round-trip
- folder creation and nested doc creation
- folder rename
- doc rename
- doc move across folders
- existing binary `fileRef` download
- binary `fileRef` upload
- uploaded `fileRef` rename
- uploaded `fileRef` move
- uploaded `fileRef` in-place replacement
- uploaded `fileRef` download
- root doc inspection
- root doc switch and restore
- compile success path
- compile log retrieval
- structured compile error analysis
- compile artifact retrieval
- PDF download

## 🧩 Architecture

This server is intentionally simple:

- `requests.Session` for authenticated HTTP access
- HTML/meta parsing for project diagnostics and discovery
- legacy realtime channel for live text editing
- MCP tools exposed through `FastMCP`

## 🔥 Why This Is Useful

With this project, MCP can work with Overleaf-like systems even when the Git-based route is a bad fit.

Examples:

- private on-prem ShareLaTeX deployments
- instances with unstable or missing Git bridge
- users who authenticate with email/password only
- workflows that need live compile status and online file management

## 📦 Quick Start

### 1. Requirements

- Python `3.10+`
- A self-hosted ShareLaTeX / Overleaf instance
- An email/password account that can access at least one project

### 2. Install

```bash
uv sync
cp .env.example .env
```

### 3. Configure environment variables

Edit `.env`:

```env
OVERLEAF_BASE_URL=http://your-overleaf-host:2233
OVERLEAF_EMAIL=your-email@example.com
OVERLEAF_PASSWORD=your-password
OVERLEAF_TIMEOUT_MS=15
OVERLEAF_ALLOW_INSECURE_HTTP=true
LOG_LEVEL=INFO
```

Environment variables:

| Variable | Required | Description |
| --- | --- | --- |
| `OVERLEAF_BASE_URL` | Yes | Base URL of your self-hosted ShareLaTeX / Overleaf instance |
| `OVERLEAF_EMAIL` | Yes | Login email |
| `OVERLEAF_PASSWORD` | Yes | Login password |
| `OVERLEAF_TIMEOUT_MS` | No | HTTP timeout in seconds. Default: `15` |
| `OVERLEAF_ALLOW_INSECURE_HTTP` | No | Set `true` if you are using plain `http://` in a trusted local network |
| `LOG_LEVEL` | No | MCP server log level. Default: `INFO` |

### 4. Smoke-test the connection

```bash
uv run python scripts/probe_login.py
uv run python scripts/probe_projects.py
```

If both commands succeed, the server can log in and discover projects correctly.

### 5. Run the MCP server

```bash
uv run sharelatex-mcp
```

The server uses the MCP `stdio` transport, so it is intended to be launched by an MCP client rather than used as a standalone web service.

### 6. Connect from an MCP client

Use a generic `stdio` MCP configuration like this and adapt it to your client:

```json
{
  "mcpServers": {
    "sharelatex": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/sharelatex-mcp",
        "sharelatex-mcp"
      ],
      "env": {
        "OVERLEAF_BASE_URL": "http://your-overleaf-host:2233",
        "OVERLEAF_EMAIL": "your-email@example.com",
        "OVERLEAF_PASSWORD": "your-password",
        "OVERLEAF_TIMEOUT_MS": "15",
        "OVERLEAF_ALLOW_INSECURE_HTTP": "true",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

If your client supports loading environment variables from a file, you can keep secrets in `.env` and only point the client to the repository directory.

### 7. Typical first-run workflow

Once connected, a good first sequence is:

1. Call `list_projects`
2. Pick a `project_id`
3. Call `list_files`
4. Read a doc with `read_file`
5. Trigger a compile with `compile_project`
6. Inspect issues with `analyze_compile_errors`

## 🧪 Validation Commands

```bash
uv run python scripts/probe_login.py
uv run python scripts/probe_projects.py
uv run python scripts/test_mcp_tools.py
uv run python scripts/test_write_roundtrip.py
uv run python scripts/test_compile_roundtrip.py
uv run python scripts/test_compile_diagnostics.py
```

## 🗂️ Tool Overview

### Project discovery

- `list_projects`
- `open_project`
- `get_project_diagnostics`

### Project structure and root doc

- `list_files`
- `get_root_doc`
- `set_root_doc`

### Text editing

- `read_file`
- `write_file`
- `create_doc`
- `create_folder`
- `rename_entity`
- `move_entity`
- `delete_entity`

### Binary assets

- `download_file`
- `upload_file`
- `replace_file`

### Compile and output inspection

- `compile_project`
- `stop_compile`
- `clear_compile_output`
- `get_compile_logs`
- `analyze_compile_errors`
- `get_compile_artifacts`
- `download_pdf`

## 📍 Positioning

If your workflow already depends on Overleaf Git sync and you want MCP to operate on a local checkout, Git-based tools are still a perfectly valid choice.

If your goal is:

- direct connection to a self-hosted ShareLaTeX / Overleaf service
- dynamic login-driven project access
- live file management and compile control

then this repository is built for that exact use case.

## 🛟 Troubleshooting

### Login loops back to `/login`

- verify `OVERLEAF_BASE_URL`
- verify the email/password pair
- confirm your instance still supports local password login

### `OVERLEAF_ALLOW_INSECURE_HTTP` error

- set `OVERLEAF_ALLOW_INSECURE_HTTP=true` only for trusted local-network `http://` deployments

### `too-recently-compiled`

- wait for the current compile cooldown to expire
- avoid triggering overlapping compile calls from multiple clients

### Realtime write errors

- retry after refreshing the project state with `read_file`
- confirm the target path is a `doc`, not a binary `fileRef`
- if your instance is heavily customized, validate write behavior with `uv run python scripts/test_write_roundtrip.py`

## 🤝 Contributing

Development setup and validation notes are in [CONTRIBUTING.md](./CONTRIBUTING.md).

## 📘 Notes

- Main README language: English
- Chinese documentation: [README_CN.md](./README_CN.md)
