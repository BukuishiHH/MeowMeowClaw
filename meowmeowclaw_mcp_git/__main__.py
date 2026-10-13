"""``python -m meowmeowclaw_mcp_git`` 入口(§10.2).

MCP SDK 走可选依赖组 ``meowmeowclaw[mcp-git]``; 未安装时给出明确 stderr 提示
并返回退出码 3, 而不是抛裸 ``ModuleNotFoundError``(§7.4 排障)。
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from .server import main as _server_main
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] == "mcp":
            print(
                "错误: 缺少 MCP SDK 依赖; 请安装: pip install 'meowmeowclaw[mcp-git]'",
                file=sys.stderr,
            )
            return 3
        raise
    return _server_main()


if __name__ == "__main__":
    raise SystemExit(main())
