"""meowmeowclaw/agent/context.py 的 Mock 单元测试.

测试策略:
- Mock 为主:
  * 用 ``fake_open`` 按路径分派文件内容(可对单个路径注入异常), 在不落盘的前提下覆盖
    "人设缺失/不可读/乱码/空白"等失败分支;
  * 用 ``MagicMock`` 顶替模块内 ``datetime``, 冻结 ``now()`` 以断言时间格式与"实时取值"语义;
- 真实文件系统为辅: 用 tmp_path 跑一遍真实读盘, 防止 Mock 假设与真实行为脱节;
- 人设已简化为"装配层传入的单一路径", 不再存在候选兜底链.

运行: pytest tests/test_context.py -v
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

import pytest

from meowmeowclaw.agent.context import (
    DEFAULT_IDENTITY,
    ContextBuilder,
)

MODULE = "meowmeowclaw.agent.context"
TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \(\w+\)")
FROZEN = datetime(2025, 3, 5, 9, 8, 7)
FROZEN_TEXT = "2025-03-05 09:08 (Wednesday)"


# --------------------------------------------------------------------- 测试替身


def fake_open(files: dict[str, Any]):
    """按路径分派的 open 替身: 内容为字符串则返回该内容, 为异常实例则抛出, 未登记则 FileNotFoundError."""

    def _open(path, *args, **kwargs):
        key = str(path)
        if key in files:
            value = files[key]
            if isinstance(value, BaseException):
                raise value
            return mock_open(read_data=value)(path, *args, **kwargs)
        raise FileNotFoundError(2, "No such file or directory", path)

    return _open


def freeze_time(*values: datetime) -> MagicMock:
    """构造可 patch 到 context 模块的 datetime 替身, 依次返回给定时间."""
    fake = MagicMock(name="datetime")
    if len(values) == 1:
        fake.now.return_value = values[0]
    else:
        fake.now.side_effect = list(values)
    return fake


# ----------------------------------------------------------------------- fixtures


@pytest.fixture
def workspace(tmp_path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def identity(tmp_path) -> Path:
    path = tmp_path / "identity.md"
    path.write_text("项目人设\n", encoding="utf-8")
    return path


@pytest.fixture
def builder(workspace, identity) -> ContextBuilder:
    return ContextBuilder(workspace, identity)


# ------------------------------------------------------------------- 构造与路径


class TestConstruction:
    def test_workspace_is_normalized_to_absolute_path(self, tmp_path, identity, monkeypatch):
        monkeypatch.chdir(tmp_path)

        b = ContextBuilder("sub/../project", identity)

        assert b.workspace.is_absolute()
        assert b.workspace == (tmp_path / "project").resolve()

    def test_identity_path_is_normalized_to_absolute_path(self, workspace, monkeypatch):
        local_identity = workspace / "identity.md"
        local_identity.write_text("本地人设", encoding="utf-8")
        monkeypatch.chdir(workspace)

        b = ContextBuilder(workspace, "identity.md")

        assert b.identity_path.is_absolute()
        assert b.identity_path == local_identity.resolve()

    def test_memory_path_is_under_workspace(self, builder, workspace):
        assert builder.memory_path == workspace / "memory" / "MEMORY.md"

    def test_accepts_str_and_path(self, workspace, identity):
        b = ContextBuilder(str(workspace), str(identity))

        assert b.workspace == workspace.resolve()
        assert b.identity_path == identity.resolve()

    def test_repr(self, builder, workspace, identity):
        text = repr(builder)

        assert str(workspace.resolve()) in text
        assert str(identity.resolve()) in text


# ------------------------------------------------------- _load_identity(单一来源)


class TestLoadIdentity:
    def test_reads_identity_file(self, builder):
        assert builder._load_identity() == "项目人设"

    def test_content_is_stripped(self, builder, identity):
        identity.write_text("  项目人设  \n\n", encoding="utf-8")

        assert builder._load_identity() == "项目人设"

    def test_missing_identity_falls_back_to_default(self, workspace, tmp_path, caplog):
        b = ContextBuilder(workspace, tmp_path / "nope.md")

        with caplog.at_level(logging.WARNING, logger=MODULE):
            content = b._load_identity()

        assert content == DEFAULT_IDENTITY
        assert "人设文件不存在" in caplog.text
        assert "nope.md" in caplog.text

    def test_blank_identity_falls_back_to_default(self, workspace, tmp_path, caplog):
        blank = tmp_path / "blank.md"
        blank.write_text("   \n\t\n", encoding="utf-8")
        b = ContextBuilder(workspace, blank)

        with caplog.at_level(logging.WARNING, logger=MODULE):
            content = b._load_identity()

        assert content == DEFAULT_IDENTITY
        assert "人设文件为空" in caplog.text

    @pytest.mark.parametrize(
        "exc",
        [PermissionError(13, "Permission denied"), IsADirectoryError(21, "Is a directory")],
    )
    def test_unreadable_identity_falls_back_to_default(self, builder, identity, caplog, exc):
        with patch("builtins.open", side_effect=fake_open({str(identity): exc})):
            with caplog.at_level(logging.WARNING, logger=MODULE):
                content = builder._load_identity()

        assert content == DEFAULT_IDENTITY
        assert "读取人设文件失败" in caplog.text

    def test_broken_encoding_falls_back_to_default(self, builder, identity, caplog):
        bad = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        with patch("builtins.open", side_effect=fake_open({str(identity): bad})):
            with caplog.at_level(logging.WARNING, logger=MODULE):
                content = builder._load_identity()

        assert content == DEFAULT_IDENTITY
        assert "UTF-8" in caplog.text

    def test_identity_is_reread_on_every_call(self, builder, identity):
        assert builder._load_identity() == "项目人设"

        identity.write_text("改过的人设", encoding="utf-8")

        assert builder._load_identity() == "改过的人设"


# --------------------------------------------------------- _load_memory(预留接口)


class TestLoadMemory:
    def test_reads_memory_from_workspace_memory_dir(self, builder, workspace):
        memory_dir = workspace / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("记住: 用户喜欢猫\n", encoding="utf-8")

        assert builder._load_memory() == "记住: 用户喜欢猫"

    def test_missing_memory_returns_empty_string(self, builder):
        assert builder._load_memory() == ""

    @pytest.mark.parametrize(
        "exc", [PermissionError(13, "Permission denied"), IsADirectoryError(21, "Is a directory")]
    )
    def test_unreadable_memory_returns_empty_string(self, builder, workspace, caplog, exc):
        memory_dir = workspace / "memory"
        memory_dir.mkdir()
        memory_path = memory_dir / "MEMORY.md"
        memory_path.write_text("x", encoding="utf-8")

        with patch("builtins.open", side_effect=fake_open({str(memory_path): exc})):
            with caplog.at_level(logging.WARNING, logger=MODULE):
                content = builder._load_memory()

        assert content == ""
        assert "读取长期记忆失败" in caplog.text

    def test_blank_memory_returns_empty_string(self, builder, workspace):
        memory_dir = workspace / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("  \n", encoding="utf-8")

        assert builder._load_memory() == ""


# ------------------------------------------------------------ build_system_prompt


class TestBuildSystemPrompt:
    def test_contains_identity_time_and_workspace(self, builder):
        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            prompt = builder.build_system_prompt()

        assert "项目人设" in prompt
        assert FROZEN_TEXT in prompt
        assert str(builder.workspace) in prompt
        assert "## 工作区" in prompt

    def test_time_is_fresh_on_every_call(self, builder):
        second = datetime(2025, 3, 6, 10, 9, 8)
        with patch(f"{MODULE}.datetime", freeze_time(FROZEN, second)):
            first_prompt = builder.build_system_prompt()
            second_prompt = builder.build_system_prompt()

        assert FROZEN_TEXT in first_prompt
        assert "2025-03-06 10:09 (Thursday)" in second_prompt

    def test_identity_is_reread_on_every_call(self, builder, identity):
        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            first_prompt = builder.build_system_prompt()
            identity.write_text("新的人设", encoding="utf-8")
            second_prompt = builder.build_system_prompt()

        assert "项目人设" in first_prompt
        assert "项目人设" not in second_prompt
        assert "新的人设" in second_prompt

    def test_default_identity_used_when_file_missing(self, workspace, tmp_path):
        b = ContextBuilder(workspace, tmp_path / "nope.md")

        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            prompt = b.build_system_prompt()

        assert DEFAULT_IDENTITY in prompt

    def test_memory_section_is_included_when_memory_exists(self, builder, workspace):
        memory_dir = workspace / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("记住: 用户喜欢猫\n", encoding="utf-8")

        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            prompt = builder.build_system_prompt()

        assert "## 长期记忆" in prompt
        assert "记住: 用户喜欢猫" in prompt

    def test_no_empty_memory_section_when_memory_absent(self, builder):
        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            prompt = builder.build_system_prompt()

        assert "## 长期记忆" not in prompt

    def test_acceptance_contains_identity_content_and_current_datetime(self, builder, identity):
        """验收标准: build_system_prompt() 同时含真实人设内容与当前日期时间."""
        with open(identity, encoding="utf-8") as f:
            expected_identity = f.read().strip()

        prompt = builder.build_system_prompt()

        assert expected_identity in prompt
        assert TIME_PATTERN.search(prompt)


# ------------------------------------------------------ 技能摘要章节(skills_summary)


class TestSkillsSummarySection:
    def test_default_is_empty(self, builder):
        assert builder.skills_summary == ""

    def test_no_section_when_summary_empty(self, builder):
        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            prompt = builder.build_system_prompt()

        assert "## 可用技能" not in prompt

    def test_summary_is_appended_at_the_end(self, workspace, identity):
        b = ContextBuilder(workspace, identity, skills_summary="- exec (exec/SKILL.md): 执行命令\n")

        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            prompt = b.build_system_prompt()

        # 传入的 summary 自带末尾换行, 拼接后保持原样
        assert prompt.endswith("## 可用技能\n- exec (exec/SKILL.md): 执行命令\n")
        assert "## 可用技能" in prompt

    def test_skills_section_comes_after_memory(self, workspace, identity):
        memory_dir = workspace / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("记忆", encoding="utf-8")
        b = ContextBuilder(workspace, identity, skills_summary="技能摘要")

        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            prompt = b.build_system_prompt()

        assert prompt.index("## 长期记忆") < prompt.index("## 可用技能")

    def test_explicit_empty_string_adds_nothing(self, workspace, identity):
        b = ContextBuilder(workspace, identity, skills_summary="")

        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            prompt = b.build_system_prompt()

        assert "## 可用技能" not in prompt

    def test_build_messages_includes_skills_in_system_prompt(self, workspace, identity):
        b = ContextBuilder(workspace, identity, skills_summary="- pdf (pdf/SKILL.md): 处理 PDF")
        messages = b.build_messages(current_message="hi")

        assert "## 可用技能" in messages[0]["content"]
        assert "pdf" in messages[0]["content"]


# ---------------------------------------------------------------- build_messages


class TestBuildMessages:
    @pytest.fixture
    def frozen(self, builder):
        with patch(f"{MODULE}.datetime", freeze_time(FROZEN)):
            yield builder

    def test_system_prompt_first_then_history_then_current(self, frozen):
        history = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
        ]

        messages = frozen.build_messages(history=history, current_message="第二问")

        assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
        assert messages[0]["content"] == frozen.build_system_prompt()
        assert messages[-1] == {"role": "user", "content": "第二问"}
        assert messages[1:-1] == history

    def test_history_is_not_mutated(self, frozen):
        history = [{"role": "user", "content": "第一问"}]
        before = list(history)

        messages = frozen.build_messages(history=history, current_message="第二问")

        assert history == before            # 入参列表未被改动
        assert messages is not history
        assert messages[1] is history[0]    # 历史条目按引用复用, 不做深拷贝

    def test_no_history(self, frozen):
        messages = frozen.build_messages(current_message="你好")

        assert [m["role"] for m in messages] == ["system", "user"]

    def test_empty_current_message_is_not_appended(self, frozen):
        messages = frozen.build_messages(history=[{"role": "user", "content": "旧"}])

        assert [m["role"] for m in messages] == ["system", "user"]  # 不追加空 user 消息

    def test_no_arguments_gives_only_system_prompt(self, frozen):
        messages = frozen.build_messages()

        assert len(messages) == 1
        assert messages[0]["role"] == "system"
        assert "项目人设" in messages[0]["content"]

    def test_acceptance_system_prompt_in_messages(self, builder, workspace, identity):
        """验收标准: messages[0] 就是完整 System Prompt(含人设与当前时间)."""
        with open(identity, encoding="utf-8") as f:
            expected_identity = f.read().strip()

        messages = builder.build_messages(current_message="hi")

        assert messages[0]["role"] == "system"
        assert expected_identity in messages[0]["content"]
        assert TIME_PATTERN.search(messages[0]["content"])
        assert str(builder.workspace) in messages[0]["content"]


# --------------------------------------------------------- 真实文件系统(防 Mock 失真)


class TestRealFilesystem:
    def test_reads_real_identity_and_memory_files(self, builder, workspace, identity):
        memory_dir = workspace / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("真实记忆", encoding="utf-8")

        prompt = builder.build_system_prompt()

        assert "项目人设" in prompt
        assert "真实记忆" in prompt

    def test_real_missing_identity_uses_default(self, workspace, tmp_path):
        b = ContextBuilder(workspace, tmp_path / "missing" / "identity.md")

        prompt = b.build_system_prompt()

        assert DEFAULT_IDENTITY in prompt

    def test_real_empty_memory_file_omits_section(self, builder, workspace):
        memory_dir = workspace / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("\n   \n", encoding="utf-8")

        prompt = builder.build_system_prompt()

        assert "## 长期记忆" not in prompt
