#!/usr/bin/env bash
# Git MCP Server 启动包装(Cline 的 mcpServers.command 可直接指向本脚本).
#
# 用法:
#   MCP_GIT_PYTHON=/path/to/python ./scripts/run_git_mcp_server.sh --root /path/to/repo --read-only=true
#
# - MCP_GIT_PYTHON: 指定解释器(需已安装 meowmeowclaw + mcp-git 依赖); 默认 python3;
# - 其余参数原样透传给 `python -m meowmeowclaw_mcp_git`;
# - 仅 POSIX; Windows 请在 Cline 配置里把 command 直接写成 python.exe 的绝对路径。
set -euo pipefail
PYTHON="${MCP_GIT_PYTHON:-python3}"
exec "$PYTHON" -m meowmeowclaw_mcp_git "$@"
