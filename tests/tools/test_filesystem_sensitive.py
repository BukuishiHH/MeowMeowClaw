"""子代理敏感路径策略在文件工具上的行为测试(SA4)."""

import pytest

from meowmeowclaw.tools.exec_policy import SensitivePathPolicy
from meowmeowclaw.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool


def make_tools(tmp_path, policy):
    workspace = tmp_path / "ws"
    memory = workspace / "memory"
    workspace.mkdir(parents=True)
    reader = ReadFileTool(workspace, memory, sensitive_policy=policy)
    writer = WriteFileTool(workspace, memory, sensitive_policy=policy)
    lister = ListDirTool(workspace, memory, sensitive_policy=policy)
    return workspace, reader, writer, lister


class TestSensitivePathInSubagentTools:
    @pytest.mark.asyncio
    async def test_read_and_write_sensitive_file_are_denied(self, tmp_path):
        policy = SensitivePathPolicy((".env", "*.pem", "id_rsa*", ".git/config"))
        workspace, reader, writer, _ = make_tools(tmp_path, policy)
        (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
        (workspace / "key.pem").write_text("pem", encoding="utf-8")

        read_env = await reader.execute(file_path=".env")
        write_env = await writer.execute(file_path=".env", content="hacked")
        read_pem = await reader.execute(file_path="key.pem")

        assert "[安全拦截]" in read_env
        assert "[安全拦截]" in write_env
        assert "[安全拦截]" in read_pem
        assert (workspace / ".env").read_text(encoding="utf-8") == "SECRET=1"

    @pytest.mark.asyncio
    async def test_list_dir_filters_sensitive_entries(self, tmp_path):
        policy = SensitivePathPolicy((".env", "*.pem"))
        workspace, _, _, lister = make_tools(tmp_path, policy)
        (workspace / ".env").write_text("x", encoding="utf-8")
        (workspace / "secret.pem").write_text("x", encoding="utf-8")
        (workspace / "main.py").write_text("print(1)", encoding="utf-8")

        result = await lister.execute(dir_path="")

        assert "main.py" in result
        assert ".env" not in result
        assert "secret.pem" not in result

    @pytest.mark.asyncio
    async def test_main_agent_without_policy_can_still_access_file(self, tmp_path):
        workspace = tmp_path / "ws"
        memory = workspace / "memory"
        workspace.mkdir(parents=True)
        (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
        reader = ReadFileTool(workspace, memory)

        assert await reader.execute(file_path=".env") == "SECRET=1"
