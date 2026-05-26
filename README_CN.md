# sharelatex-mcp

> 🚀 一个面向自部署 ShareLaTeX / Overleaf 的 Python MCP 服务。  
> 直接连接在线服务本体，动态读取项目，不依赖 Git bridge。

<p align="left">
  <a href="./README.md">🇬🇧 English README</a>
</p>

## ✨ 这是什么？

`sharelatex-mcp` 不是把 Overleaf 当成一个 Git 仓库来同步，而是把它当成一个真实在线服务来接入。

它会像正常用户一样：

- 用邮箱和密码登录
- 读取项目列表和项目元数据
- 调用项目 HTTP 接口
- 通过 legacy realtime 通道写入文本内容

这意味着它特别适合下面这些场景：

- 你用的是自部署 ShareLaTeX / Overleaf
- 你没有稳定可用的 Git bridge
- 你不想把项目先同步到本地仓库再交给 MCP
- 你希望 MCP 直接操作线上项目本身

## 🧭 它和常见 Overleaf MCP 的区别

很多公开方案本质上是 “Git-first”：

- 先通过 Git bridge 拿到仓库
- 再在本地改文件
- 再 commit / push 回 Overleaf

这个项目解决的不是那条链路，而是另一条链路：

- 直接登录自部署实例
- 动态列出当前账号下的项目
- 直接对在线项目做读写、管理和编译操作

## ⚡ 和基于 Git 的 Overleaf MCP 的差异

| 能力 | `sharelatex-mcp` | 常见 Git 型 Overleaf MCP |
| --- | --- | --- |
| 接入方式 | 直接走 Web session | 走本地 Git 同步 |
| 是否依赖 Git bridge | 否 | 通常依赖 |
| 是否要预先写死项目映射 | 否 | 往往需要 |
| 登录后动态列项目 | 支持 | 往往较弱 |
| 在线实时文本编辑 | 支持 | 间接完成 |
| 在线编译控制 | 支持 | 往往不支持 |
| 二进制资源上传下载 | 支持 | 通常依赖 Git |
| 面向自部署 ShareLaTeX | 是 | 不一定 |

## 🛠️ 当前已实现能力

### 项目访问

- `list_projects`
- `open_project`
- `get_project_diagnostics`
- `get_root_doc`
- `set_root_doc`
- `list_files`

### 文本文件工作流

- `read_file`
- `write_file`
- `create_doc`
- `create_folder`
- `rename_entity`
- `move_entity`
- `delete_entity`

### 二进制资源工作流

- `download_file`
- `upload_file`
- `replace_file`

### 编译工作流

- `compile_project`
- `stop_compile`
- `clear_compile_output`
- `get_compile_logs`
- `analyze_compile_errors`
- `get_compile_artifacts`
- `download_pdf`

## ✅ 已在真实自部署实例验证

下面这些链路都已经对真实 ShareLaTeX 派生实例跑通过：

- 邮箱密码登录
- 动态项目发现
- 文本文件读写闭环
- 文件夹创建与子目录文档创建
- 文件夹重命名
- 文档重命名
- 文档跨目录移动
- 已有二进制 `fileRef` 下载
- 二进制 `fileRef` 上传
- 上传后的 `fileRef` 重命名
- 上传后的 `fileRef` 移动
- 上传后的 `fileRef` 原位替换
- 上传后的 `fileRef` 下载
- 主编译文件读取
- 主编译文件切换与恢复
- 编译成功链路
- 编译日志读取
- 结构化编译诊断
- 编译产物读取
- PDF 下载

## 📦 快速开始

### 1. 环境要求

- Python `3.10+`
- 一个自部署 ShareLaTeX / Overleaf 实例
- 一个能访问至少一个项目的邮箱密码账号

### 2. 安装

```bash
uv sync
cp .env.example .env
```

### 3. 配置环境变量

编辑 `.env`：

```env
OVERLEAF_BASE_URL=http://your-overleaf-host:2233
OVERLEAF_EMAIL=your-email@example.com
OVERLEAF_PASSWORD=your-password
OVERLEAF_TIMEOUT_MS=15
OVERLEAF_ALLOW_INSECURE_HTTP=true
LOG_LEVEL=INFO
```

变量说明：

| 变量名 | 必填 | 说明 |
| --- | --- | --- |
| `OVERLEAF_BASE_URL` | 是 | 你的自部署 ShareLaTeX / Overleaf 基础地址 |
| `OVERLEAF_EMAIL` | 是 | 登录邮箱 |
| `OVERLEAF_PASSWORD` | 是 | 登录密码 |
| `OVERLEAF_TIMEOUT_MS` | 否 | HTTP 超时秒数，默认 `15` |
| `OVERLEAF_ALLOW_INSECURE_HTTP` | 否 | 若你在可信局域网中使用 `http://`，设为 `true` |
| `LOG_LEVEL` | 否 | MCP 服务日志级别，默认 `INFO` |

### 4. 先做连通性验证

```bash
uv run python scripts/probe_login.py
uv run python scripts/probe_projects.py
```

如果这两条命令都成功，说明登录和项目发现链路是通的。

### 5. 启动 MCP 服务

```bash
uv run sharelatex-mcp
```

这个服务使用 MCP 的 `stdio` 传输方式，因此更适合作为 MCP 客户端的后端进程来启动，而不是单独作为 Web 服务运行。

### 6. 接入 MCP 客户端

可以先用下面这个通用 `stdio` MCP 配置示例，再按你的客户端格式做适配：

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

如果你的 MCP 客户端支持从 `.env` 加载环境变量，也可以把密码继续保留在 `.env` 中，只在客户端里指向仓库目录即可。

### 7. 首次使用推荐顺序

接入成功后，推荐先按这个顺序试：

1. 调用 `list_projects`
2. 选择一个 `project_id`
3. 调用 `list_files`
4. 用 `read_file` 读取一个文档
5. 用 `compile_project` 触发编译
6. 用 `analyze_compile_errors` 查看结构化问题

## 🧪 验证命令

```bash
uv run python scripts/probe_login.py
uv run python scripts/probe_projects.py
uv run python scripts/test_mcp_tools.py
uv run python scripts/test_write_roundtrip.py
uv run python scripts/test_compile_roundtrip.py
uv run python scripts/test_compile_diagnostics.py
```

## 🗂️ 工具概览

### 项目发现

- `list_projects`
- `open_project`
- `get_project_diagnostics`

### 项目结构与主文件

- `list_files`
- `get_root_doc`
- `set_root_doc`

### 文本编辑

- `read_file`
- `write_file`
- `create_doc`
- `create_folder`
- `rename_entity`
- `move_entity`
- `delete_entity`

### 二进制资源

- `download_file`
- `upload_file`
- `replace_file`

### 编译与产物检查

- `compile_project`
- `stop_compile`
- `clear_compile_output`
- `get_compile_logs`
- `analyze_compile_errors`
- `get_compile_artifacts`
- `download_pdf`

## 🧩 技术设计

整个服务刻意保持轻量：

- 用 `requests.Session` 维护登录态
- 用 HTML / meta 信息解析项目页面
- 用 legacy realtime 通道做在线文本写入
- 用 `FastMCP` 暴露 MCP 工具

## 🔥 为什么这个项目有价值

如果你已经非常依赖 Git bridge，那 Git-first 方案仍然很合适。

但在这些场景下，这个项目会更直接：

- 私有内网部署
- Git bridge 不稳定或根本没开
- 用户只有邮箱密码登录方式
- 你需要直接拿到线上编译状态和在线文件管理能力

## 📍 项目定位

如果你的需求是：

- 让 MCP 操作一个本地同步下来的 Overleaf Git 仓库

那 Git 型方案依然合理。

如果你的需求是：

- 直接连接自部署 ShareLaTeX / Overleaf 服务本体
- 登录后自动列出项目
- 直接管理线上文件和编译

那这个仓库就是为这条路线设计的。

## 🛟 常见问题

### 登录后还是跳回 `/login`

- 检查 `OVERLEAF_BASE_URL` 是否正确
- 检查邮箱和密码是否正确
- 确认你的实例仍然支持本地邮箱密码登录

### 报 `OVERLEAF_ALLOW_INSECURE_HTTP` 错误

- 如果你在可信局域网里用的是 `http://`，请显式设置 `OVERLEAF_ALLOW_INSECURE_HTTP=true`

### 遇到 `too-recently-compiled`

- 等待当前编译冷却时间结束
- 避免多个客户端同时重复触发编译

### realtime 写入失败

- 先用 `read_file` 刷新一次当前文档状态后再试
- 确认目标路径是 `doc`，不是二进制 `fileRef`
- 如果你的实例做过较多自定义，先用 `uv run python scripts/test_write_roundtrip.py` 验证写入链路

## 🤝 参与开发

开发环境准备和回归验证说明见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 📘 说明

- 主 README：英文 [`README.md`](./README.md)
- 中文说明：当前文件 [`README_CN.md`](./README_CN.md)
