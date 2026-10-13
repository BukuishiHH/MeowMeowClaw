"""MeowMeowClaw Git MCP Server(设计与里程碑见 ``docs/GIT_MCP_DESIGN.md``).

独立于主 Agent 的 Git MCP Server:

- stdio 传输(JSON-RPC), 由 Cline 等 MCP Host 启动;
- 唯一执行后端是 ``git`` CLI(参数列表、无 shell);
- 默认只读, 写/远端能力需显式开关;
- 本包 import 无副作用, 且**不依赖 MCP SDK** —— SDK 只在
  ``meowmeowclaw_mcp_git.server`` 中导入, 便于配置/仓库/解析层独立单测(G7)。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
