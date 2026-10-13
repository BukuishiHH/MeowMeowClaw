#!/usr/bin/env python3
"""GM6 端到端验收脚本（人工运行，不进 CI）。

覆盖：

1. 临时仓库 + 本地 bare remote（不依赖网络）；
2. 只读模式：13 个工具、status/diff/log、prompt、resource；
3. 可写模式：21 个工具、stage→commit（真实提交）、`--audit-log` JSONL 与 0600 权限；
4. 远端模式：22 个工具、fetch 到另一 clone 的新提交（refs_changed≥1，且不合并）；
5. stdout 纯净性：空 stdin 启动退出后 stdout 必须为 0 字节；
6. 退出后无残留 server/git 子进程。

用法：
    python scripts/verify_git_mcp_server.py [--keep]

退出码：0=全部通过，1=有失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover - 提示安装可选依赖
    print("缺少 MCP SDK：请先执行 pip install -e '.[mcp-git]'", file=sys.stderr)
    raise SystemExit(1)


GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_EDITOR": "true",
    "LC_ALL": "C",
    "PYTHONPATH": str(PROJECT_ROOT),
}

READ_TOOL_COUNT = 13
WRITE_TOOL_COUNT = 21
REMOTE_TOOL_COUNT = 22


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failures.append(name)
            print(f"  FAIL  {name}  {detail}".rstrip())

    def summary(self) -> int:
        total = self.passed + len(self.failures)
        if self.failures:
            print(f"\n验收失败：{len(self.failures)}/{total} 项未通过")
            for item in self.failures:
                print(f"  - {item}")
            return 1
        print(f"\n全部通过：{self.passed}/{total} 项")
        return 0


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        timeout=60,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {completed.stderr.strip()}")
    return completed


def server_params(repo: Path, *extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "meowmeowclaw_mcp_git", "--root", str(repo), *extra],
        env={**os.environ, **GIT_ENV},
    )


def build_fixture(root: Path) -> tuple[Path, Path, Path]:
    """建 bare remote + work clone(分支 work, upstream origin/main) + other clone."""
    remote = root / "origin.git"
    run_git(root, "init", "--bare", "-q", "-b", "main", str(remote))
    work = root / "work"
    work.mkdir()
    run_git(work, "init", "-q", "-b", "work")
    for key, value in (("user.name", "E2E"), ("user.email", "e2e@example.com"), ("commit.gpgsign", "false")):
        run_git(work, "config", key, value)
    (work / "app.py").write_text("line1\nline2\n", encoding="utf-8")
    run_git(work, "add", "app.py")
    run_git(work, "commit", "-qm", "chore: init")
    run_git(work, "remote", "add", "origin", str(remote))
    run_git(work, "push", "-q", "-u", "origin", "work:main")

    other = root / "other"
    run_git(root, "clone", "-q", str(remote), str(other))
    for key, value in (("user.name", "Other"), ("user.email", "other@example.com"), ("commit.gpgsign", "false")):
        run_git(other, "config", key, value)

    # 预置一个未提交修改, 供 diff/prompt 使用
    (work / "app.py").write_text("line1 changed\nline2\n", encoding="utf-8")
    return work, remote, other


async def phase_read_only(work: Path, checks: Checks) -> None:
    print("\n[1/5] 只读模式")
    async with stdio_client(server_params(work, "--read-only=true")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            checks.check("只读模式工具数=13", len(names) == READ_TOOL_COUNT, f"实际 {len(names)}")
            checks.check("写/远端工具不可见", "git_commit" not in names and "git_fetch" not in names)

            status = json.loads((await session.call_tool("git_status", {})).content[0].text)
            checks.check("git_status 返回 ok", status["ok"] and status["data"]["branch"] == "work")
            checks.check("git_status 看到未暂存修改", status["data"]["unstaged"] == ["app.py"])

            diff = json.loads((await session.call_tool("git_diff", {"stat_only": True})).content[0].text)
            checks.check(
                "git_diff 返回 app.py 统计",
                diff["ok"] and [item["path"] for item in diff["data"]["files"]] == ["app.py"],
            )

            log = json.loads((await session.call_tool("git_log", {"max_count": 5})).content[0].text)
            checks.check("git_log 返回提交", log["ok"] and log["data"]["count"] == 1)

            prompt = await session.get_prompt("write_commit_message", {"staged_only": "false"})
            checks.check("prompt 包含工作区改动", "line1 changed" in prompt.messages[0].content.text)

            resource = await session.read_resource(f"git://{work.name}/head")
            checks.check("head resource 可读", json.loads(resource.contents[0].text)["branch"] == "work")
            file_resource = await session.read_resource(f"git://{work.name}/file/HEAD/app.py")
            checks.check("file resource 回原文", "line1" in file_resource.contents[0].text)


async def phase_writable(work: Path, audit_path: Path, checks: Checks) -> None:
    print("\n[2/5] 可写模式 + 审计")
    async with stdio_client(server_params(work, "--read-only=false", "--audit-log", str(audit_path))) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            checks.check("可写模式工具数=21", len(names) == WRITE_TOOL_COUNT, f"实际 {len(names)}")

            staged = json.loads((await session.call_tool("git_stage", {"paths": ["app.py"]})).content[0].text)
            committed = json.loads((await session.call_tool("git_commit", {"message": "feat: e2e change"})).content[0].text)
            checks.check("stage+commit 成功", staged["ok"] and committed["ok"] and committed["data"]["after"]["dirty_files"] == 0)
            checks.check(
                "提交真实落库",
                run_git(work, "log", "-1", "--format=%s").stdout.strip() == "feat: e2e change",
            )

    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
    checks.check("审计 JSONL 已写入", len(events) >= 2, f"实际 {len(events)} 条")
    checks.check("审计含工具/状态/摘要", events and {"tool", "status", "args_digest", "exit_code"} <= set(events[0]))
    checks.check("审计不落盘提交 message", "e2e change" not in audit_path.read_text(encoding="utf-8"))
    if os.name != "nt":
        checks.check("审计文件权限 0600", audit_path.stat().st_mode & 0o777 == 0o600)


async def phase_remote(work: Path, other: Path, checks: Checks) -> None:
    print("\n[3/5] 远端模式(fetch)")
    (other / "remote.txt").write_text("remote\n", encoding="utf-8")
    run_git(other, "add", "remote.txt")
    run_git(other, "commit", "-qm", "feat: remote work")
    run_git(other, "push", "-q", "origin", "main")

    async with stdio_client(server_params(work, "--allow-remote=true", "--read-only=false")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            checks.check("远端模式工具数=22", len(names) == REMOTE_TOOL_COUNT, f"实际 {len(names)}")
            checks.check("git_fetch 已注册", "git_fetch" in names)

            fetch = json.loads((await session.call_tool("git_fetch", {"remote": "origin"})).content[0].text)
            refs = {item["name"]: item for item in fetch["data"]["refs"]} if fetch["ok"] else {}
            checks.check("fetch 更新 origin/main", fetch["ok"] and refs.get("origin/main", {}).get("change") == "updated")
            checks.check("fetch 不合并本地分支", run_git(work, "log", "-1", "--format=%s").stdout.strip() == "feat: e2e change")


def phase_stdout_purity(work: Path, checks: Checks) -> None:
    print("\n[4/5] stdout 纯净性")
    completed = subprocess.run(
        [sys.executable, "-m", "meowmeowclaw_mcp_git", "--root", str(work)],
        input=b"",
        capture_output=True,
        timeout=60,
        env={**os.environ, **GIT_ENV},
    )
    checks.check("空 stdin 退出码 0", completed.returncode == 0, f"实际 {completed.returncode}")
    checks.check("stdout 0 字节", completed.stdout == b"", f"实际 {completed.stdout[:60]!r}")
    checks.check("stderr 有启动/退出日志", "启动" in completed.stderr.decode("utf-8", "replace"))


def phase_no_leftovers(checks: Checks) -> None:
    print("\n[5/5] 残留进程检查")
    pgrep = shutil.which("pgrep")
    if pgrep is None:  # pragma: no cover - 平台没有 pgrep
        checks.check("pgrep 不可用, 跳过", True)
        return
    time.sleep(0.3)
    completed = subprocess.run([pgrep, "-af", "meowmeowclaw_mcp_git"], capture_output=True, text=True)
    leftovers = [line for line in completed.stdout.splitlines() if line.strip()]
    checks.check("无残留 server 进程", not leftovers, "; ".join(leftovers[:3]))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Git MCP Server 端到端验收")
    parser.add_argument("--keep", action="store_true", help="保留临时目录(排障用)")
    args = parser.parse_args()

    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="mcp-git-e2e-") as temp:
        root = Path(temp)
        work, _remote, other = build_fixture(root)
        audit_path = root / "audit.jsonl"
        print(f"临时仓库: {root}")
        await phase_read_only(work, checks)
        await phase_writable(work, audit_path, checks)
        await phase_remote(work, other, checks)
        phase_stdout_purity(work, checks)
        phase_no_leftovers(checks)
        if args.keep:
            keep_dir = Path(tempfile.mkdtemp(prefix="mcp-git-e2e-keep-"))
            shutil.copytree(root, keep_dir, dirs_exist_ok=True)
            print(f"已保留副本: {keep_dir}")
    return checks.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
