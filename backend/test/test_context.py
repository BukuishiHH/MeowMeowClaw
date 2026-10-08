"""backend/agent/context.py 的 Mock 单元测试.

测试策略:
- Mock 为主:
  * 用 ``mock_open`` 替身按路径分派文件内容(可对单个路径注入异常), 在不落盘的前提下覆盖
    "工作区人设存在/缺失、backend 兜底、两者皆无、不可读、乱码、空白"等分支,
    并断言读取的**精确路径与编码**;
  * 用 ``MagicMock`` 顶替模块内 ``datetime``, 冻结 `now()` 以断言时间格式与"实时取值"语义;
- 真实文件系统为辅: 用 tmp_path 跑一遍真实读盘, 防止 Mock 假设与真实行为脱节;
- 验收标准: build_system_prompt() 必须同时含"人设内容"与"当前日期时间".

运行: pytest backend/test/test_context.py -v
"""

import os
import re
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

import pytest

import backend.agent.context as context_module
from backend.agent.context import (
    DEFAULT_IDENTITY,
    FALLBACK_IDENTITY_DIR,
    ContextBuilder,
)

MODULE = "backend.agent.context"
TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \(\w+\)")
FROZEN = datetime(2025, 3, 5, 9, 8, 7)
FROZEN_TEXT = "2025-03-05 09:08 (Wednesday)"
BACKEND_IDENTITY = os.path.join(FALLBACK_IDENTITY_DIR, "identity.md")


# --------------------------------------------------------------------- 测试替身


def fake_open(files: dict[str, Any]):
    """按路径分派的 open 替身: 内容为字符串则返回该内容, 为异常实例则抛出, 未登记则 FileNotFoundError."""

    def _open(path, *args, **kwargs):
        if path in files:
            value = files[path]
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


@pytest.fixture
def workspace(tmp_path) -> str:
    return str(tmp_path)


@pytest.fixture
def workspace_identity(workspace) -> str:
    """首选人设路径: workspace/identity.md"""
    return os.path.join(workspace, "identity.md")


# ------------------------------------------------------------------- 构造与路径


class TestConstruction:
    def test_workspace_is_normalized_to_absolute_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        b = ContextBuilder("sub/../project")

        assert os.path.isabs(b.workspace)
        assert b.workspace == os.path.abspath("sub/../project")

    def test_default_identity_lookup_order(self, workspace, workspace_identity):
        b = ContextBuilder(workspace)

        assert b.identity_file == "identity.md"
        assert b.identity_path == workspace_identity                       # 首选: 工作区
        assert b.fallback_identity_path == BACKEND_IDENTITY                # 兜底: backend/
        assert b.identity_candidates == (workspace_identity, BACKEND_IDENTITY)
        assert os.path.isfile(b.fallback_identity_path)                    # 项目自带兜底人设真实存在

    def test_absolute_identity_file_is_used_as_is(self, workspace, tmp_path):
        absolute = str(tmp_path / "custom" / "persona.md")

        b = ContextBuilder(workspace, identity_file=absolute)

        assert b.identity_path == absolute
        assert b.fallback_identity_path == os.path.join(FALLBACK_IDENTITY_DIR, "persona.md")

    def test_custom_relative_identity_file(self, workspace):
        b = ContextBuilder(workspace, identity_file="persona/meow.md")

        assert b.identity_path == os.path.join(workspace, "persona/meow.md")
        assert b.fallback_identity_path == os.path.join(FALLBACK_IDENTITY_DIR, "meow.md")

    def test_memory_path_is_under_workspace(self, workspace):
        assert ContextBuilder(workspace).memory_path == os.path.join(
            workspace, "memory", "MEMORY.md"
        )

    def test_repr(self, workspace):
        text = repr(ContextBuilder(workspace))

        assert "ContextBuilder" in text
        assert workspace in text
        assert "identity.md" in text


# --------------------------------------------- _load_identity: 工作区 → backend 兜底


class TestLoadIdentity:
    def test_workspace_identity_wins_and_fallback_is_not_read(
        self, workspace, workspace_identity
    ):
        opener = MagicMock(
            side_effect=fake_open({workspace_identity: "工作区人设\n", BACKEND_IDENTITY: "兜底人设"})
        )

        with patch("builtins.open", opener):
            content = ContextBuilder(workspace)._load_identity()

        assert content == "工作区人设"  # 首尾空白被裁掉
        opener.assert_any_call(workspace_identity, "r", encoding="utf-8")
        assert BACKEND_IDENTITY not in [c.args[0] for c in opener.call_args_list]

    def test_falls_back_to_backend_identity_when_workspace_file_missing(
        self, workspace, workspace_identity
    ):
        opener = MagicMock(side_effect=fake_open({BACKEND_IDENTITY: "项目自带人设"}))

        with patch("builtins.open", opener):
            content = ContextBuilder(workspace)._load_identity()

        assert content == "项目自带人设"
        # 两个候选都被尝试过, 且顺序是先工作区后 backend
        assert [c.args[0] for c in opener.call_args_list] == [
            workspace_identity,
            BACKEND_IDENTITY,
        ]

    def test_default_when_both_candidates_missing(self, workspace, workspace_identity):
        opener = MagicMock(side_effect=fake_open({}))

        with patch("builtins.open", opener):
            content = ContextBuilder(workspace)._load_identity()

        assert content == DEFAULT_IDENTITY
        assert [c.args[0] for c in opener.call_args_list] == [
            workspace_identity,
            BACKEND_IDENTITY,
        ]

    def test_blank_workspace_identity_falls_through_to_backend(self, workspace, workspace_identity):
        files = {workspace_identity: "   \n\t", BACKEND_IDENTITY: "兜底人设"}

        with patch("builtins.open", side_effect=fake_open(files)):
            assert ContextBuilder(workspace)._load_identity() == "兜底人设"

    def test_unreadable_workspace_identity_falls_through_to_backend(
        self, workspace, workspace_identity
    ):
        files = {workspace_identity: PermissionError(13, "Permission denied"), BACKEND_IDENTITY: "兜底人设"}

        with patch("builtins.open", side_effect=fake_open(files)):
            assert ContextBuilder(workspace)._load_identity() == "兜底人设"

    def test_broken_encoding_workspace_identity_falls_through_to_backend(
        self, workspace, workspace_identity
    ):
        bad = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        files = {workspace_identity: bad, BACKEND_IDENTITY: "兜底人设"}

        with patch("builtins.open", side_effect=fake_open(files)):
            assert ContextBuilder(workspace)._load_identity() == "兜底人设"

    @pytest.mark.parametrize(
        "exc",
        [
            PermissionError(13, "Permission denied"),
            IsADirectoryError(21, "Is a directory"),
        ],
    )
    def test_default_when_all_candidates_raise(self, workspace, exc):
        with patch("builtins.open", side_effect=exc):
            assert ContextBuilder(workspace)._load_identity() == DEFAULT_IDENTITY

    def test_custom_identity_file_is_read_from_workspace(self, workspace, tmp_path):
        custom = str(tmp_path / "persona.md")
        with patch("builtins.open", side_effect=fake_open({custom: "自定义人设"})):
            assert ContextBuilder(workspace, custom)._load_identity() == "自定义人设"

    def test_fallback_is_reread_on_every_call(self, workspace, workspace_identity):
        # 运行中往工作区放入人设文件, 应立即生效并压过 backend 兜底
        files = {BACKEND_IDENTITY: "兜底人设"}

        with patch("builtins.open", side_effect=fake_open(files)):
            b = ContextBuilder(workspace)
            assert b._load_identity() == "兜底人设"
            files[workspace_identity] = "新放入的工作区人设"
            assert b._load_identity() == "新放入的工作区人设"


# --------------------------------------------------------- _load_memory(预留接口)


class TestLoadMemory:
    def test_reads_memory_from_workspace_memory_dir(self, workspace):
        expected_path = os.path.join(workspace, "memory", "MEMORY.md")
        opener = MagicMock(side_effect=fake_open({expected_path: "用户偏好中文\n"}))

        with patch("builtins.open", opener):
            memory = ContextBuilder(workspace)._load_memory()

        assert memory == "用户偏好中文"
        opener.assert_any_call(expected_path, "r", encoding="utf-8")

    def test_missing_memory_returns_empty_string(self, workspace):
        with patch("builtins.open", side_effect=FileNotFoundError(2, "No such file")):
            assert ContextBuilder(workspace)._load_memory() == ""

    @pytest.mark.parametrize(
        "exc",
        [PermissionError(13, "Permission denied"), IsADirectoryError(21, "Is a directory")],
    )
    def test_unreadable_memory_returns_empty_string(self, workspace, exc):
        with patch("builtins.open", side_effect=exc):
            assert ContextBuilder(workspace)._load_memory() == ""

    def test_blank_memory_returns_empty_string(self, workspace):
        expected_path = os.path.join(workspace, "memory", "MEMORY.md")
        with patch("builtins.open", side_effect=fake_open({expected_path: "  \n "})):
            assert ContextBuilder(workspace)._load_memory() == ""


# ------------------------------------------------------------ build_system_prompt


class TestBuildSystemPrompt:
    def test_contains_identity_time_and_workspace(self, workspace, workspace_identity):
        files = {workspace_identity: "你是喵喵爪"}
        with patch("builtins.open", side_effect=fake_open(files)), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN)
        ):
            prompt = ContextBuilder(workspace).build_system_prompt()

        assert "你是喵喵爪" in prompt
        assert FROZEN_TEXT in prompt
        assert workspace in prompt

    def test_workspace_identity_overrides_backend_fallback(self, workspace, workspace_identity):
        files = {workspace_identity: "工作区人设", BACKEND_IDENTITY: "项目兜底人设"}
        with patch("builtins.open", side_effect=fake_open(files)), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN)
        ):
            prompt = ContextBuilder(workspace).build_system_prompt()

        assert "工作区人设" in prompt
        assert "项目兜底人设" not in prompt

    def test_backend_identity_used_when_workspace_has_none(self, workspace):
        with patch("builtins.open", side_effect=fake_open({BACKEND_IDENTITY: "项目兜底人设"})), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN)
        ):
            prompt = ContextBuilder(workspace).build_system_prompt()

        assert "项目兜底人设" in prompt

    def test_memory_section_is_included_when_memory_exists(self, workspace, workspace_identity):
        memory_path = os.path.join(workspace, "memory", "MEMORY.md")
        files = {workspace_identity: "人设", memory_path: "用户喜欢简洁回答"}

        with patch("builtins.open", side_effect=fake_open(files)), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN)
        ):
            prompt = ContextBuilder(workspace).build_system_prompt()

        assert "## 长期记忆" in prompt
        assert "用户喜欢简洁回答" in prompt

    def test_no_empty_memory_section_when_memory_absent(self, workspace, workspace_identity):
        with patch("builtins.open", side_effect=fake_open({workspace_identity: "人设"})), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN)
        ):
            prompt = ContextBuilder(workspace).build_system_prompt()

        assert "## 长期记忆" not in prompt  # 不给模型留空章节

    def test_time_is_fresh_on_every_call(self, workspace, workspace_identity):
        later = datetime(2025, 3, 5, 18, 30, 0)

        with patch("builtins.open", side_effect=fake_open({workspace_identity: "人设"})), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN, later)
        ) as fake_dt:
            b = ContextBuilder(workspace)
            first = b.build_system_prompt()
            second = b.build_system_prompt()

        assert FROZEN_TEXT in first
        assert "2025-03-05 18:30 (Wednesday)" in second  # 不是构造期快照
        assert fake_dt.now.call_count == 2

    def test_identity_is_reread_on_every_call(self, workspace, workspace_identity):
        files = {workspace_identity: "旧人设"}

        with patch("builtins.open", side_effect=fake_open(files)), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN)
        ):
            b = ContextBuilder(workspace)
            first = b.build_system_prompt()
            files[workspace_identity] = "新人设"  # 运行中热改人设
            second = b.build_system_prompt()

        assert "旧人设" in first
        assert "新人设" in second

    def test_default_identity_used_when_all_candidates_missing(self, workspace):
        with patch("builtins.open", side_effect=FileNotFoundError(2, "No such file")), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN)
        ):
            prompt = ContextBuilder(workspace).build_system_prompt()

        assert DEFAULT_IDENTITY in prompt

    def test_acceptance_contains_identity_content_and_current_datetime(self, workspace):
        """验收标准: 输出同时包含真实人设内容与当前日期时间(工作区无人设, 走 backend 兜底)."""
        b = ContextBuilder(workspace)
        with open(b.fallback_identity_path, encoding="utf-8") as f:
            expected_identity = f.read().strip()

        prompt = b.build_system_prompt()

        assert expected_identity in prompt   # 人设内容
        assert TIME_PATTERN.search(prompt)   # 当前日期时间
        assert b.workspace in prompt         # 工作区路径


# ------------------------------------------------------ 技能摘要章节(skills_summary)


class TestSkillsSummarySection:
    def test_default_is_empty(self, workspace):
        assert ContextBuilder(workspace).skills_summary == ""

    def test_no_section_when_summary_empty(self, workspace):
        prompt = ContextBuilder(workspace).build_system_prompt()

        assert "## 可用技能" not in prompt

    def test_summary_is_appended_at_the_end(self, workspace):
        summary = (
            "你有以下技能可用. 请先用 read_file 读取.\n\n"
            "可用技能:\n- pdf (pdf/SKILL.md): 处理 PDF\n"
        )

        prompt = ContextBuilder(workspace, skills_summary=summary).build_system_prompt()

        assert prompt.endswith("\n\n## 可用技能\n" + summary)
        assert prompt.count("## 可用技能") == 1

    def test_skills_section_comes_after_memory(self, workspace):
        memory_dir = os.path.join(workspace, "memory")
        os.makedirs(memory_dir, exist_ok=True)
        with open(os.path.join(memory_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("长期记忆内容")

        prompt = ContextBuilder(workspace, skills_summary="技能摘要").build_system_prompt()

        assert prompt.index("## 长期记忆") < prompt.index("## 可用技能")
        assert "长期记忆内容" in prompt and "技能摘要" in prompt

    def test_explicit_empty_string_adds_nothing(self, workspace):
        assert "## 可用技能" not in ContextBuilder(workspace, skills_summary="").build_system_prompt()

    def test_positional_argument_compatibility(self, workspace):
        # 老写法(位置参数)仍然可用: 第三个位置现在放技能摘要
        builder = ContextBuilder(workspace, "identity.md", "技能摘要")

        assert builder.skills_summary == "技能摘要"
        assert builder.identity_file == "identity.md"

    def test_build_messages_includes_skills_in_system_prompt(self, workspace):
        messages = ContextBuilder(workspace, skills_summary="技能摘要").build_messages(
            current_message="hi"
        )

        assert messages[0]["role"] == "system"
        assert "## 可用技能" in messages[0]["content"]


# ---------------------------------------------------------------- build_messages


class TestBuildMessages:
    @pytest.fixture
    def frozen(self, workspace, workspace_identity):
        with patch("builtins.open", side_effect=fake_open({workspace_identity: "人设"})), patch(
            f"{MODULE}.datetime", freeze_time(FROZEN)
        ):
            yield ContextBuilder(workspace)

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
        assert "人设" in messages[0]["content"]

    def test_acceptance_system_prompt_in_messages(self, workspace):
        """验收标准: messages[0] 就是完整的 System Prompt(含人设与当前时间)."""
        b = ContextBuilder(workspace)
        with open(b.fallback_identity_path, encoding="utf-8") as f:
            expected_identity = f.read().strip()

        messages = b.build_messages(current_message="hi")

        assert messages[0]["role"] == "system"
        assert expected_identity in messages[0]["content"]
        assert TIME_PATTERN.search(messages[0]["content"])
        assert b.workspace in messages[0]["content"]


# --------------------------------------------------------- 真实文件系统(防 Mock 失真)


class TestRealFilesystem:
    def test_workspace_identity_beats_backend_fallback_on_real_disk(self, workspace):
        with open(os.path.join(workspace, "identity.md"), "w", encoding="utf-8") as f:
            f.write("真实工作区人设")

        prompt = ContextBuilder(workspace).build_system_prompt()

        with open(BACKEND_IDENTITY, encoding="utf-8") as f:
            backend_identity = f.read().strip()

        assert "真实工作区人设" in prompt
        assert backend_identity not in prompt

    def test_uses_backend_identity_when_workspace_has_none(self, workspace):
        prompt = ContextBuilder(workspace).build_system_prompt()

        with open(BACKEND_IDENTITY, encoding="utf-8") as f:
            assert f.read().strip() in prompt

    def test_reads_real_identity_and_memory_files(self, workspace, tmp_path):
        identity = tmp_path / "persona.md"
        identity.write_text("真实人设: 你是测试助手", encoding="utf-8")
        memory_dir = os.path.join(workspace, "memory")
        os.makedirs(memory_dir, exist_ok=True)
        with open(os.path.join(memory_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("真实记忆: 用户名叫小明")

        prompt = ContextBuilder(workspace, identity_file=str(identity)).build_system_prompt()

        assert "真实人设: 你是测试助手" in prompt
        assert "## 长期记忆" in prompt
        assert "真实记忆: 用户名叫小明" in prompt

    def test_real_empty_memory_file_omits_section(self, workspace):
        memory_dir = os.path.join(workspace, "memory")
        os.makedirs(memory_dir, exist_ok=True)
        open(os.path.join(memory_dir, "MEMORY.md"), "w", encoding="utf-8").close()

        prompt = ContextBuilder(workspace).build_system_prompt()

        assert "## 长期记忆" not in prompt
