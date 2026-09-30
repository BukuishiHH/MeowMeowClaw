"""backend/agent/tools/filesystem.py 的 Mock 单元测试.

被测对象: ReadFileTool(read_file) / WriteFileTool(write_file) / ListDirTool(list_dir).

测试策略:
- Mock 为主: 用 ``unittest.mock.patch`` / ``monkeypatch`` 伪造 OS 层调用
  (builtins.open / os.makedirs / os.listdir / os.path.isdir / os.path.getsize),
  在不真正读写磁盘的前提下稳定复现 FileNotFoundError、IsADirectoryError、PermissionError、
  磁盘故障等用真实文件系统难以触发(以 root 运行时几乎无法触发)的分支,
  并断言"被安全拦截时不得产生任何 IO 副作用";
- 真实文件系统为辅: 关键路径用 tmp_path 再跑一遍, 校验 Mock 的假设与 OS 真实语义一致,
  防止 Mock 与真实契约脱节;
- 契约校验: name / description / parameters(OpenAI JSON Schema) 与 BaseTool 抽象契约.
  说明: 路径校验用的是 ``str.startswith``, 存在同前缀兄弟目录绕过缺口, 见 TestKnownGaps.

运行: pytest backend/test/test_filesystem.py -v
"""

import inspect
import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from backend.agent.tools import BaseTool
from backend.agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from backend.agent.tools.registry import ToolRegistry

# 与实现对齐的常量: 截断阈值与截断提示(用于边界断言)
TRUNCATE_LIMIT = 16000
TRUNCATE_NOTICE = "\n\n==== 内容已截断, 超过16000字符 ===="

TOOL_CLASSES = (ReadFileTool, WriteFileTool, ListDirTool)
EXPECTED_TOOL_NAMES = {
    ReadFileTool: "read_file",
    WriteFileTool: "write_file",
    ListDirTool: "list_dir",
}
EXPECTED_REQUIRED_PARAMS = {
    ReadFileTool: ("file_path",),
    WriteFileTool: ("file_path", "content"),
    ListDirTool: ("dir_path",),
}


def resolve(workspace: str, rel_path: str) -> str:
    """与实现同构的路径解析, 用于断言工具真正访问的绝对路径."""
    return os.path.abspath(os.path.join(workspace, rel_path))


def write_text(path: str, content: str) -> None:
    """真实落盘小工具(仅在真实文件系统用例中使用)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------- 公共 fixture


@pytest.fixture(params=TOOL_CLASSES, ids=lambda cls: cls.__name__)
def tool_cls(request):
    """参数化: 依次把三个工具类喂给用例."""
    return request.param


@pytest.fixture
def workspace(tmp_path) -> str:
    """隔离的工作区根目录(真实临时目录, Mock 用例中也只当字符串用)."""
    return str(tmp_path)


@pytest.fixture
def tool(tool_cls, workspace) -> BaseTool:
    """按参数化的工具类构造实例, 工作区固定为临时目录."""
    return tool_cls(workspace)


# ------------------------------------------------------------ 工具契约(三工具通用)


class TestToolContract:
    """三个工具对 BaseTool / LLM 的对外契约, 不依赖任何 IO."""

    def test_is_base_tool_subclass_with_expected_name(self, tool, tool_cls):
        assert isinstance(tool, BaseTool)
        assert tool.name == EXPECTED_TOOL_NAMES[tool_cls]
        assert tool.name.islower()
        assert " " not in tool.name

    def test_label_defaults_to_name_and_strict_is_false(self, tool):
        assert tool.label == tool.name
        assert tool.strict is False

    def test_description_is_llm_readable(self, tool):
        assert isinstance(tool.description, str)
        assert len(tool.description.strip()) >= 20

    def test_parameters_is_valid_openai_json_schema(self, tool, tool_cls):
        schema = tool.parameters
        expected_params = EXPECTED_REQUIRED_PARAMS[tool_cls]

        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert tuple(schema["required"]) == expected_params
        assert set(schema["properties"]) == set(expected_params)
        for param, prop in schema["properties"].items():
            assert prop["type"] == "string", param
            assert prop["description"].strip(), param

    def test_parameters_returns_fresh_dict_each_call(self, tool):
        assert tool.parameters == tool.parameters
        assert tool.parameters is not tool.parameters

    def test_to_function_definition_is_llm_ready(self, tool):
        definition = tool.to_function_definition()

        assert definition["type"] == "function"
        assert definition["function"]["name"] == tool.name
        assert definition["function"]["description"] == tool.description
        assert definition["function"]["parameters"] == tool.parameters
        assert definition["function"]["strict"] is False

    def test_execute_is_async(self, tool):
        assert inspect.iscoroutinefunction(tool.execute)

    def test_workspace_is_normalized_to_absolute_path(self, tool_cls, tmp_path, monkeypatch):
        # 传入相对工作区时, 构造阶段即归一化为绝对路径(路径防护的前提)
        monkeypatch.chdir(tmp_path)
        relative_tool = tool_cls("sub/../nested")

        assert os.path.isabs(relative_tool.workspace)
        assert relative_tool.workspace == os.path.abspath("sub/../nested")


# ------------------------------------------------ ReadFileTool: Mock 掉 builtins.open


class TestReadFileMocked:
    """用 mock_open / side_effect 替换 builtins.open, 全程不产生真实文件 IO."""

    @pytest.mark.asyncio
    async def test_reads_relative_path_as_utf8_text(self, workspace):
        mock = mock_open(read_data="你好, MeowMeowClaw")
        with patch("builtins.open", mock):
            result = await ReadFileTool(workspace).execute(file_path="src/main.py")

        assert isinstance(result, str)
        assert result == "你好, MeowMeowClaw"
        mock.assert_called_once_with(resolve(workspace, "src/main.py"), "r", encoding="utf-8")

    @pytest.mark.asyncio
    async def test_missing_file_path_kwarg_falls_back_to_workspace_root(self, workspace):
        # 不传 file_path 时取默认值 "", 解析结果等于工作区根目录
        mock = mock_open(read_data="")
        with patch("builtins.open", mock):
            await ReadFileTool(workspace).execute()

        mock.assert_called_once_with(resolve(workspace, ""), "r", encoding="utf-8")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content_len", "truncated"),
        [
            (1, False),
            (TRUNCATE_LIMIT - 1, False),
            (TRUNCATE_LIMIT, False),  # 边界: 刚好 16000 字符不截断
            (TRUNCATE_LIMIT + 1, True),  # 边界: 16001 字符截断
            (TRUNCATE_LIMIT * 3, True),
        ],
    )
    async def test_truncates_only_when_content_exceeds_limit(self, workspace, content_len, truncated):
        payload = "a" * content_len
        mock = mock_open(read_data=payload)
        with patch("builtins.open", mock):
            result = await ReadFileTool(workspace).execute(file_path="big.txt")

        if truncated:
            assert result == "a" * TRUNCATE_LIMIT + TRUNCATE_NOTICE
        else:
            assert result == payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "prefix"),
        [
            (FileNotFoundError(2, "No such file or directory"), "[错误] 文件不存在: "),
            (IsADirectoryError(21, "Is a directory"), "[错误]给定路径是目录, 不是文件: "),
        ],
    )
    async def test_expected_os_errors_return_text_with_absolute_path(self, workspace, exc, prefix):
        with patch("builtins.open", side_effect=exc):
            result = await ReadFileTool(workspace).execute(file_path="a.txt")

        assert isinstance(result, str)
        assert result.startswith(prefix)
        assert result.endswith(resolve(workspace, "a.txt"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            PermissionError(13, "Permission denied"),
            UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte"),
            OSError("磁盘故障"),
            RuntimeError("未知异常"),
        ],
    )
    async def test_unexpected_open_errors_are_wrapped_with_repr(self, workspace, exc):
        with patch("builtins.open", side_effect=exc):
            result = await ReadFileTool(workspace).execute(file_path="a.txt")

        assert result == f"[读取文件异常] {exc!r}"

    @pytest.mark.asyncio
    async def test_decode_error_raised_by_read_is_wrapped(self, workspace):
        # UnicodeDecodeError 实际由 f.read() 抛出, 不应冒泡到 Agent
        exc = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        handle = MagicMock()
        handle.__enter__.return_value = handle
        handle.__exit__.return_value = False  # 必须为假值, 否则 with 会吞掉异常
        handle.read.side_effect = exc

        with patch("builtins.open", return_value=handle):
            result = await ReadFileTool(workspace).execute(file_path="gbk.txt")

        assert result == f"[读取文件异常] {exc!r}"
        handle.read.assert_called_once_with()


# ------------------------------------------------ WriteFileTool: Mock 掉 open/makedirs


class TestWriteFileMocked:
    """用 Mock 校验写入参数、父目录创建与异常包装, 不真正落盘."""

    @pytest.mark.asyncio
    async def test_creates_parent_dirs_then_writes_utf8(self, workspace):
        mock = mock_open()
        with patch("builtins.open", mock), patch("os.makedirs") as makedirs:
            result = await WriteFileTool(workspace).execute(
                file_path="pkg/deep/mod.py", content="print('喵')"
            )

        assert result == "[成功] 文件已写入: pkg/deep/mod.py"
        makedirs.assert_called_once_with(
            os.path.dirname(resolve(workspace, "pkg/deep/mod.py")), exist_ok=True
        )
        mock.assert_called_once_with(resolve(workspace, "pkg/deep/mod.py"), "w", encoding="utf-8")
        # 用 return_value 而非 mock() 取句柄, 避免额外记一次调用
        mock.return_value.write.assert_called_once_with("print('喵')")

    @pytest.mark.asyncio
    async def test_root_level_file_still_calls_makedirs_with_exist_ok(self, workspace):
        with patch("builtins.open", mock_open()), patch("os.makedirs") as makedirs:
            await WriteFileTool(workspace).execute(file_path="a.py", content="x")

        makedirs.assert_called_once_with(workspace, exist_ok=True)

    @pytest.mark.asyncio
    async def test_missing_content_kwarg_writes_empty_string(self, workspace):
        mock = mock_open()
        with patch("builtins.open", mock), patch("os.makedirs"):
            result = await WriteFileTool(workspace).execute(file_path="empty.txt")

        assert result == "[成功] 文件已写入: empty.txt"
        mock.return_value.write.assert_called_once_with("")

    @pytest.mark.asyncio
    async def test_target_is_directory_returns_readable_error(self, workspace):
        with patch("builtins.open", side_effect=IsADirectoryError(21, "Is a directory")), patch(
            "os.makedirs"
        ):
            result = await WriteFileTool(workspace).execute(file_path="pkg", content="x")

        assert result == f"[错误] 目标路径是目录, 不能作为文件: {resolve(workspace, 'pkg')}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [OSError("磁盘已满"), PermissionError(13, "Permission denied")])
    async def test_write_errors_are_wrapped_with_repr(self, workspace, exc):
        with patch("builtins.open", side_effect=exc), patch("os.makedirs"):
            result = await WriteFileTool(workspace).execute(file_path="a.py", content="x")

        assert result == f"[写入文件异常] {exc!r}"

    @pytest.mark.asyncio
    async def test_makedirs_failure_is_wrapped_and_open_never_called(self, workspace):
        exc = PermissionError(13, "Permission denied")
        open_mock = MagicMock()
        with patch("builtins.open", open_mock), patch("os.makedirs", side_effect=exc):
            result = await WriteFileTool(workspace).execute(file_path="a/b.py", content="x")

        assert result == f"[写入文件异常] {exc!r}"
        open_mock.assert_not_called()  # 建目录失败就不该再尝试打开文件


# ------------------------------------------------ ListDirTool: Mock 掉 os.listdir 等


class TestListDirMocked:
    """monkeypatch 伪造 os.listdir / os.path.isdir / os.path.getsize, 隔离真实目录内容."""

    @pytest.mark.asyncio
    async def test_formats_sorted_entries_from_mocked_os(self, workspace, monkeypatch):
        subdir = os.path.join(workspace, "src")
        py_file = os.path.join(workspace, "a.py")
        md_file = os.path.join(workspace, "b.md")

        listdir = MagicMock(return_value=["src", "b.md", "a.py"])  # 故意乱序
        monkeypatch.setattr(os, "listdir", listdir)
        monkeypatch.setattr(os.path, "isdir", lambda p: p in (workspace, subdir))
        monkeypatch.setattr(os.path, "getsize", lambda p: {py_file: 12, md_file: 34}[p])

        result = await ListDirTool(workspace).execute(dir_path="")

        assert result == "a.py  size=12 bytes\nb.md  size=34 bytes\nsrc/"
        listdir.assert_called_once_with(resolve(workspace, ""))

    @pytest.mark.asyncio
    async def test_empty_directory_returns_placeholder(self, workspace, monkeypatch):
        monkeypatch.setattr(os, "listdir", MagicMock(return_value=[]))

        assert await ListDirTool(workspace).execute(dir_path="") == "目录为空"

    @pytest.mark.asyncio
    async def test_permission_error_is_wrapped(self, workspace, monkeypatch):
        # 以 root 运行时真实文件系统几乎无法触发 PermissionError, 只能靠 Mock 稳定覆盖
        listdir = MagicMock(side_effect=PermissionError(13, "Permission denied"))
        monkeypatch.setattr(os, "listdir", listdir)

        result = await ListDirTool(workspace).execute(dir_path="")

        assert result == f"[错误] 无权限读取目录: {resolve(workspace, '')}"
        listdir.assert_called_once_with(resolve(workspace, ""))

    @pytest.mark.asyncio
    async def test_unexpected_error_is_wrapped_with_repr(self, workspace, monkeypatch):
        exc = RuntimeError("目录服务内核故障")
        monkeypatch.setattr(os, "listdir", MagicMock(side_effect=exc))

        result = await ListDirTool(workspace).execute(dir_path="")

        assert result == f"[列举目录异常] {exc!r}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_path", ["..", "../etc", "/etc"])
    async def test_traversal_is_blocked_without_touching_disk(self, workspace, monkeypatch, bad_path):
        listdir = MagicMock(return_value=[])
        monkeypatch.setattr(os, "listdir", listdir)

        result = await ListDirTool(workspace).execute(dir_path=bad_path)

        assert result == f"[安全拦截] 禁止列出工作区外目录, 请求路径: {bad_path}"
        listdir.assert_not_called()


# ------------------------------------------------------------ 路径防护(Mock 断言无副作用)


class TestPathGuardWithMocks:
    """路径穿越必须在触达 OS 之前被拦截: 用 Mock 断言"零 IO 副作用"."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_path", ["../secret.txt", "nested/../../secret.txt", "/etc/passwd", ".."]
    )
    async def test_read_file_blocked_before_open(self, workspace, bad_path):
        open_mock = MagicMock()
        with patch("builtins.open", open_mock):
            result = await ReadFileTool(workspace).execute(file_path=bad_path)

        assert result == f"[安全拦截] 禁止访问工作区外路径, 请求路径: {bad_path}"
        open_mock.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_path", ["../evil.txt", "sub/../../evil.txt", "/etc/passwd"])
    async def test_write_file_blocked_before_any_io(self, workspace, bad_path):
        open_mock = MagicMock()
        makedirs = MagicMock()
        with patch("builtins.open", open_mock), patch("os.makedirs", makedirs):
            result = await WriteFileTool(workspace).execute(file_path=bad_path, content="pwned")

        assert result == f"[安全拦截] 禁止写入工作区外路径, 请求路径: {bad_path}"
        open_mock.assert_not_called()
        makedirs.assert_not_called()


# --------------------------------------------------- 真实文件系统用例(防止 Mock 失真)


class TestReadFileRealFilesystem:
    @pytest.mark.asyncio
    async def test_reads_utf8_file(self, workspace):
        write_text(os.path.join(workspace, "docs", "说明.md"), "喵喵喵\nline2")

        assert await ReadFileTool(workspace).execute(file_path="docs/说明.md") == "喵喵喵\nline2"

    @pytest.mark.asyncio
    async def test_missing_file_returns_error_text(self, workspace):
        result = await ReadFileTool(workspace).execute(file_path="nope.txt")

        assert result == f"[错误] 文件不存在: {resolve(workspace, 'nope.txt')}"

    @pytest.mark.asyncio
    async def test_real_directory_target_raises_isadirectory(self, workspace):
        # 交叉验证 Mock 用例的假设: Linux 下 open(目录) 确实抛 IsADirectoryError
        os.makedirs(os.path.join(workspace, "pkg"))

        result = await ReadFileTool(workspace).execute(file_path="pkg")

        assert result == f"[错误]给定路径是目录, 不是文件: {resolve(workspace, 'pkg')}"


class TestWriteFileRealFilesystem:
    @pytest.mark.asyncio
    async def test_writes_file_creating_parent_dirs(self, workspace):
        result = await WriteFileTool(workspace).execute(file_path="a/b/c.txt", content="hello 喵")

        assert result == "[成功] 文件已写入: a/b/c.txt"
        with open(os.path.join(workspace, "a", "b", "c.txt"), encoding="utf-8") as f:
            assert f.read() == "hello 喵"

    @pytest.mark.asyncio
    async def test_existing_file_is_overwritten(self, workspace):
        tool = WriteFileTool(workspace)
        await tool.execute(file_path="x.txt", content="first")
        await tool.execute(file_path="x.txt", content="second")

        with open(os.path.join(workspace, "x.txt"), encoding="utf-8") as f:
            assert f.read() == "second"

    @pytest.mark.asyncio
    async def test_real_directory_target_is_rejected(self, workspace):
        os.makedirs(os.path.join(workspace, "dir_target"))

        result = await WriteFileTool(workspace).execute(file_path="dir_target", content="x")

        assert result == f"[错误] 目标路径是目录, 不能作为文件: {resolve(workspace, 'dir_target')}"


class TestListDirRealFilesystem:
    @pytest.mark.asyncio
    async def test_lists_root_sorted_with_dir_suffix_and_size(self, workspace):
        os.makedirs(os.path.join(workspace, "src"))
        write_text(os.path.join(workspace, "a.txt"), "hello")  # 5 字节
        write_text(os.path.join(workspace, "b.md"), "x")  # 1 字节

        result = await ListDirTool(workspace).execute(dir_path="")

        assert result == "a.txt  size=5 bytes\nb.md  size=1 bytes\nsrc/"

    @pytest.mark.asyncio
    async def test_size_is_byte_count_not_char_count(self, workspace):
        write_text(os.path.join(workspace, "cn.txt"), "喵喵喵")  # 3 字符 / 9 字节

        assert await ListDirTool(workspace).execute(dir_path="") == "cn.txt  size=9 bytes"

    @pytest.mark.asyncio
    async def test_lists_subdirectory(self, workspace):
        write_text(os.path.join(workspace, "src", "main.py"), "pass\n")

        assert await ListDirTool(workspace).execute(dir_path="src") == "main.py  size=5 bytes"

    @pytest.mark.asyncio
    async def test_empty_directory_returns_placeholder(self, workspace):
        assert await ListDirTool(workspace).execute(dir_path="") == "目录为空"

    @pytest.mark.asyncio
    async def test_file_target_is_not_a_directory(self, workspace):
        write_text(os.path.join(workspace, "a.txt"), "x")

        result = await ListDirTool(workspace).execute(dir_path="a.txt")

        assert result == f"[错误] 路径不是有效目录: {resolve(workspace, 'a.txt')}"

    @pytest.mark.asyncio
    async def test_nonexistent_directory(self, workspace):
        result = await ListDirTool(workspace).execute(dir_path="nope")

        assert result == f"[错误] 路径不是有效目录: {resolve(workspace, 'nope')}"


# ------------------------------------------------------------------ 与 Registry 联调


class TestRegistryIntegration:
    """三个工具经 ToolRegistry 路由执行, 确认 name 与 execute 契约端到端可用."""

    @pytest.mark.asyncio
    async def test_three_tools_end_to_end(self, workspace):
        registry = ToolRegistry()
        for tool_cls in TOOL_CLASSES:
            registry.register(tool_cls(workspace))

        assert registry.list_tools() == ["read_file", "write_file", "list_dir"]

        content = "1. 第一项\n"
        assert (
            await registry.execute("write_file", {"file_path": "notes/todo.md", "content": content})
            == "[成功] 文件已写入: notes/todo.md"
        )
        assert await registry.execute("read_file", {"file_path": "notes/todo.md"}) == content
        assert (
            await registry.execute("list_dir", {"dir_path": "notes"})
            == f"todo.md  size={len(content.encode('utf-8'))} bytes"
        )


# ------------------------------------------------------------- 已知缺口(xfail 跟踪)


class TestKnownGaps:
    """以 xfail 记录当前实现的已知缺口: 修复后自动转 XPASS, 便于回归跟踪."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["read", "write", "list"])
    @pytest.mark.xfail(
        reason="路径校验用 str.startswith, 同前缀兄弟目录 ws_evil 被误判为工作区内 "
        "(应改用 os.path.commonpath 或 Path.relative_to)",
        strict=False,
    )
    async def test_sibling_dir_sharing_workspace_prefix_should_be_blocked(self, tmp_path, action):
        workspace_dir = tmp_path / "ws"
        workspace_dir.mkdir()
        sibling = tmp_path / "ws_evil"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("top-secret", encoding="utf-8")

        workspace = str(workspace_dir)
        if action == "read":
            result = await ReadFileTool(workspace).execute(file_path="../ws_evil/secret.txt")
        elif action == "write":
            result = await WriteFileTool(workspace).execute(
                file_path="../ws_evil/pwned.txt", content="pwned"
            )
        else:
            result = await ListDirTool(workspace).execute(dir_path="../ws_evil")

        assert "[安全拦截]" in result

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="路径拼接在 try 之外, 非字符串路径(如 LLM 传 null)会向上抛 TypeError, "
        "违背 BaseTool '异常在内部捕获' 的建议",
        strict=False,
    )
    async def test_non_string_path_should_return_text_instead_of_raising(self, workspace):
        result = await ReadFileTool(workspace).execute(file_path=None)

        assert "[读取文件异常]" in result
