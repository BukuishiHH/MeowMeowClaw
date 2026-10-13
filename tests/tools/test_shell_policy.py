"""ExecTool SA4 策略/取消/锁测试.

覆盖 docs/SUBAGENT_DESIGN.md §6.5/§7.3:
- 环境覆盖只暴露 allowlist 变量;
- command_policy 拒绝后台/strict 组合;
- CancelledError 杀进程组, 不留孤儿;
- mutation_guard 锁超时转工具错误。
"""

import asyncio
import os

import pytest

from meowmeowclaw.tools.exec_policy import SubagentCommandPolicy, build_subagent_env
from meowmeowclaw.tools.mutation import WorkspaceMutationGuard
from meowmeowclaw.tools.shell import ExecTool


def subagent_env(tmp_path) -> dict[str, str]:
    return build_subagent_env(home_dir=tmp_path / "home", allowlist=())


class TestExecEnvAndPolicy:
    @pytest.mark.asyncio
    async def test_env_override_is_used_for_child_process(self, tmp_path):
        env = subagent_env(tmp_path)
        env["MY_MARKER"] = "subagent-env"
        tool = ExecTool(str(tmp_path), env=env)

        result = await tool.execute(
            command='python3 -c "import os; print(os.environ.get(\'MY_MARKER\', \'MISSING\'))"'
        )

        assert "subagent-env" in result
        assert "MISSING" not in result

    @pytest.mark.asyncio
    async def test_command_policy_denies_background_before_spawn(self, tmp_path):
        tool = ExecTool(
            str(tmp_path),
            env=subagent_env(tmp_path),
            command_policy=SubagentCommandPolicy(mode="guarded"),
        )

        result = await tool.execute(command="nohup sleep 1 &")

        assert "[安全拦截]" in result
        assert "后台" in result or "守护" in result

    @pytest.mark.asyncio
    async def test_allowlist_policy_denies_shell_control(self, tmp_path):
        tool = ExecTool(
            str(tmp_path),
            env=subagent_env(tmp_path),
            command_policy=SubagentCommandPolicy(
                mode="allowlist", allowlist=("pytest -q",)
            ),
        )

        result = await tool.execute(command="pytest -q ; echo hacked")

        assert "[安全拦截]" in result

    def test_for_subagent_produces_new_instance_with_shared_guard(self, tmp_path):
        guard = WorkspaceMutationGuard(tmp_path / "workspace.lock")
        base = ExecTool(str(tmp_path), mutation_guard=guard)

        child = base.for_subagent(
            env=subagent_env(tmp_path),
            command_policy=SubagentCommandPolicy(mode="guarded"),
        )

        assert child is not base
        assert child._mutation_guard is guard  # noqa: SLF001


class TestExecCancellation:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组语义")
    @pytest.mark.asyncio
    async def test_cancelled_command_kills_process_group(self, tmp_path):
        pid_path = tmp_path / "pid.txt"
        env = subagent_env(tmp_path)
        env["PATH"] = os.environ.get("PATH", os.defpath)
        tool = ExecTool(str(tmp_path), env=env)
        command = f"sh -c 'echo $$ > {pid_path}; exec sleep 30'"

        task = asyncio.create_task(tool.execute(command=command))
        pid = None
        for _ in range(100):
            if pid_path.exists():
                text = pid_path.read_text(encoding="utf-8").strip()
                if text.isdigit():
                    pid = int(text)
                    break
            await asyncio.sleep(0.02)
        assert pid is not None, "子进程未写出 pid"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 等待进程被回收/杀掉
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("取消后子进程仍存活")

    @pytest.mark.asyncio
    async def test_mutation_lock_timeout_returns_tool_error(self, tmp_path):
        lock_path = tmp_path / "workspace.lock"
        holder = WorkspaceMutationGuard(lock_path, timeout=1.0)
        waiter = WorkspaceMutationGuard(lock_path, timeout=0.05)
        tool = ExecTool(str(tmp_path), mutation_guard=waiter)
        release = asyncio.Event()

        async def hold() -> None:
            async with holder.mutate(reason="hold"):
                await release.wait()

        task = asyncio.create_task(hold())
        await asyncio.sleep(0.01)

        result = await tool.execute(command="echo hi")

        assert "[变更锁超时]" in result

        release.set()
        await task
