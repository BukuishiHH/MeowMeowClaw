"""meowmeowclaw/tools/web_search.py 的 Mock 单元测试.

测试策略:
- 全部离线: 用替身 ``DDGS`` 顶掉真实搜索, 断言"传给搜索库的参数"与"结果格式化",
  不产生任何网络请求;
- 替身会记录 **执行线程名**, 用来验证 ``DDGS().text()`` 确实被 ``asyncio.to_thread``
  丢进了工作线程(而不是阻塞事件循环);
- 覆盖超时、异常、无结果、结果结构异常、超长截断、依赖缺失等分支.

运行: pytest tests/test_web_search.py -v
"""

import asyncio
import logging
import os
import socket
import threading
import time
from typing import Any, Optional

import pytest

import meowmeowclaw.tools.web_search as web_module
from meowmeowclaw.tools import BaseTool
from meowmeowclaw.tools.web_search import WebSearchTool
from meowmeowclaw.tools.registry import ToolRegistry
from meowmeowclaw.tools.web_search import (
    DEFAULT_MAX_RESULTS,
    MAX_OUTPUT_CHARS,
    MAX_RESULTS_LIMIT,
    TRUNCATE_NOTICE,
)

# ---------------------------------------------------------------- 联网测试开关
# 真实联网用例默认跳过: 离线/CI 环境跑全量套件时不受影响;
# 需要检测连通性时执行: RUN_NETWORK_TESTS=1 pytest tests/test_web_search.py -m network -v -s
RUN_NETWORK_TESTS = os.getenv("RUN_NETWORK_TESTS") == "1"
network_only = pytest.mark.skipif(
    not RUN_NETWORK_TESTS,
    reason="真实联网用例: 需外网, 设 RUN_NETWORK_TESTS=1 开启",
)
TCP_PROBE_TIMEOUT = 5.0

SPEC_DESCRIPTION = "搜索互联网获取最新信息. 当你需要查询实时信息、最新新闻或不确定的知识时使用."

SAMPLE_RESULTS = [
    {
        "title": "MeowMeowClaw 项目主页",
        "href": "https://example.com/meow",
        "body": "一个不依赖编排框架的自定义 Agent.",
    },
    {
        "title": "第二篇结果",
        "href": "https://example.com/second",
        "body": "摘要二",
    },
]


# --------------------------------------------------------------------- 测试替身


_UNSET = object()  # 区分"没传 results"与"显式传 None(模拟搜索库返回 None)"


def make_fake_ddgs(
    results: Any = _UNSET,
    error: Optional[BaseException] = None,
    sleep_seconds: float = 0.0,
):
    """构造替身 DDGS 类, 并返回 (FakeDDGS, calls) 供断言."""
    calls: list[dict[str, Any]] = []
    payload = SAMPLE_RESULTS if results is _UNSET else results

    class FakeDDGS:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.init_args = args
            self.init_kwargs = kwargs

        def text(self, query: str, **kwargs: Any) -> list[Any]:
            calls.append(
                {
                    "query": query,
                    **kwargs,
                    "thread": threading.current_thread().name,
                    "instance": self,
                }
            )
            if sleep_seconds:
                time.sleep(sleep_seconds)
            if error is not None:
                raise error
            return payload

    return FakeDDGS, calls


@pytest.fixture
def tool() -> WebSearchTool:
    return WebSearchTool()


# ------------------------------------------------------------------ 工具契约


class TestToolContract:
    def test_spec_constants(self):
        """规格写死的数值必须钉字面量: 默认 5 条、上限 8000 字符."""
        assert DEFAULT_MAX_RESULTS == 5
        assert MAX_OUTPUT_CHARS == 8000

    def test_is_base_tool_with_expected_name(self, tool):
        assert isinstance(tool, BaseTool)
        assert tool.name == "web_search"
        assert tool.label == tool.name  # 默认复用 name

    def test_description_matches_spec_verbatim(self, tool):
        assert tool.description == SPEC_DESCRIPTION

    def test_parameters_schema(self, tool):
        schema = tool.parameters

        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["query"]
        assert schema["properties"]["query"]["type"] == "string"
        assert schema["properties"]["max_results"]["type"] == "integer"
        assert schema["properties"]["max_results"]["default"] == DEFAULT_MAX_RESULTS

    def test_to_function_definition_is_llm_ready(self, tool):
        definition = tool.to_function_definition()

        assert definition["type"] == "function"
        assert definition["function"]["name"] == "web_search"
        assert definition["function"]["parameters"] == tool.parameters
        assert definition["function"]["strict"] is False

    def test_strict_and_execute_contract(self, tool):
        assert asyncio.iscoroutinefunction(tool.execute)
        assert "web_search" in repr(tool)


# ---------------------------------------------------------- 结果格式化(离线)


class TestResultFormatting:
    @pytest.mark.asyncio
    async def test_single_result_format(self, tool, monkeypatch):
        fake, _ = make_fake_ddgs(results=[SAMPLE_RESULTS[0]])
        monkeypatch.setattr(web_module, "DDGS", fake)

        result = await tool.execute(query="MeowMeowClaw")

        assert result == (
            "### 1. MeowMeowClaw 项目主页\n"
            "链接: https://example.com/meow\n"
            "一个不依赖编排框架的自定义 Agent.\n"
        )

    @pytest.mark.asyncio
    async def test_multiple_results_keep_order_and_numbering(self, tool, monkeypatch):
        fake, _ = make_fake_ddgs(results=SAMPLE_RESULTS)
        monkeypatch.setattr(web_module, "DDGS", fake)

        result = await tool.execute(query="q")

        assert result.index("### 1.") < result.index("### 2.")
        assert "### 1. MeowMeowClaw 项目主页" in result
        assert "### 2. 第二篇结果" in result
        assert "链接: https://example.com/second" in result
        assert "摘要二" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("item", "expected"),
        [
            ({"title": "只有标题"}, "### 1. 只有标题\n链接: \n\n"),
            ({"href": "https://只有链接"}, "### 1. \n链接: https://只有链接\n\n"),
            ({"body": "只有摘要"}, "### 1. \n链接: \n只有摘要\n"),
            ({}, "### 1. \n链接: \n\n"),
        ],
    )
    async def test_missing_fields_are_tolerated(self, tool, monkeypatch, item, expected):
        fake, _ = make_fake_ddgs(results=[item])
        monkeypatch.setattr(web_module, "DDGS", fake)

        assert await tool.execute(query="q") == expected

    @pytest.mark.asyncio
    async def test_non_dict_entries_are_skipped(self, tool, monkeypatch):
        fake, _ = make_fake_ddgs(results=[None, "字符串", {"title": "有效", "href": "h", "body": "b"}])
        monkeypatch.setattr(web_module, "DDGS", fake)

        result = await tool.execute(query="q")

        assert result == "### 1. 有效\n链接: h\nb\n"  # 序号从 1 连续, 不跳号

    @pytest.mark.asyncio
    async def test_truncates_over_limit(self, tool, monkeypatch):
        fake, _ = make_fake_ddgs(
            results=[{"title": "长文", "href": "https://x", "body": "x" * 9000}]
        )
        monkeypatch.setattr(web_module, "DDGS", fake)

        result = await tool.execute(query="q")

        assert len(result) == MAX_OUTPUT_CHARS + len(TRUNCATE_NOTICE)
        assert result.endswith(TRUNCATE_NOTICE)

    @pytest.mark.asyncio
    async def test_exactly_at_limit_is_not_truncated(self, tool, monkeypatch):
        # 构造"正好 8000 字符"的单条结果
        body = "y" * (MAX_OUTPUT_CHARS - len("### 1. t\n链接: h\n\n"))
        fake, _ = make_fake_ddgs(results=[{"title": "t", "href": "h", "body": body}])
        monkeypatch.setattr(web_module, "DDGS", fake)

        result = await tool.execute(query="q")

        assert len(result) == MAX_OUTPUT_CHARS
        assert TRUNCATE_NOTICE not in result


# ------------------------------------------------------------------ 入参处理


class TestParameters:
    @pytest.mark.asyncio
    async def test_query_and_max_results_forwarded(self, tool, monkeypatch):
        fake, calls = make_fake_ddgs()
        monkeypatch.setattr(web_module, "DDGS", fake)

        await tool.execute(query="python asyncio", max_results=3)

        assert calls[0]["query"] == "python asyncio"
        assert calls[0]["max_results"] == 3

    @pytest.mark.asyncio
    async def test_missing_max_results_uses_default(self, tool, monkeypatch):
        fake, calls = make_fake_ddgs()
        monkeypatch.setattr(web_module, "DDGS", fake)

        await tool.execute(query="q")

        assert calls[0]["max_results"] == DEFAULT_MAX_RESULTS

    @pytest.mark.asyncio
    async def test_string_number_is_coerced(self, tool, monkeypatch):
        fake, calls = make_fake_ddgs()
        monkeypatch.setattr(web_module, "DDGS", fake)

        await tool.execute(query="q", max_results="7")

        assert calls[0]["max_results"] == 7

    @pytest.mark.asyncio
    async def test_invalid_max_results_falls_back_with_warning(self, tool, monkeypatch, caplog):
        fake, calls = make_fake_ddgs()
        monkeypatch.setattr(web_module, "DDGS", fake)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.tools.web_search"):
            await tool.execute(query="q", max_results="很多条")

        assert calls[0]["max_results"] == DEFAULT_MAX_RESULTS
        assert any("max_results" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("given", "expected"), [(0, 1), (-5, 1), (1000, MAX_RESULTS_LIMIT)])
    async def test_max_results_is_clamped(self, tool, monkeypatch, given, expected):
        fake, calls = make_fake_ddgs()
        monkeypatch.setattr(web_module, "DDGS", fake)

        await tool.execute(query="q", max_results=given)

        assert calls[0]["max_results"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [{}, {"query": ""}, {"query": "   "}, {"query": None}])
    async def test_empty_query_is_rejected_without_searching(self, tool, monkeypatch, kwargs):
        fake, calls = make_fake_ddgs()
        monkeypatch.setattr(web_module, "DDGS", fake)

        result = await tool.execute(**kwargs)

        assert result == "[错误] 搜索关键词不能为空"
        assert calls == []  # 没触发任何搜索

    @pytest.mark.asyncio
    async def test_query_is_stripped(self, tool, monkeypatch):
        fake, calls = make_fake_ddgs()
        monkeypatch.setattr(web_module, "DDGS", fake)

        await tool.execute(query="  spaced query  ")

        assert calls[0]["query"] == "spaced query"


# ------------------------------------------------------- 空结果 / 异常 / 超时


class TestEmptyAndErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("results", [[], None])
    async def test_no_results_returns_placeholder(self, tool, monkeypatch, results):
        fake, _ = make_fake_ddgs(results=results)
        monkeypatch.setattr(web_module, "DDGS", fake)

        assert await tool.execute(query="q") == "未找到相关结果"

    @pytest.mark.asyncio
    async def test_all_entries_invalid_returns_placeholder(self, tool, monkeypatch):
        fake, _ = make_fake_ddgs(results=[None, "x", 42])
        monkeypatch.setattr(web_module, "DDGS", fake)

        assert await tool.execute(query="q") == "未找到相关结果"

    @pytest.mark.asyncio
    async def test_search_exception_returns_error_text(self, tool, monkeypatch, caplog):
        fake, _ = make_fake_ddgs(error=RuntimeError("网络不可达"))
        monkeypatch.setattr(web_module, "DDGS", fake)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.tools.web_search"):
            result = await tool.execute(query="q")

        assert result == "搜索出错: 网络不可达"
        assert any("搜索失败" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_ddgs_constructor_error_is_wrapped(self, tool, monkeypatch):
        class BoomDDGS:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise OSError("无法初始化客户端")

        monkeypatch.setattr(web_module, "DDGS", BoomDDGS)

        assert await tool.execute(query="q") == "搜索出错: 无法初始化客户端"

    @pytest.mark.asyncio
    async def test_timeout_returns_error_text_and_returns_quickly(self, tool, monkeypatch, caplog):
        fake, _ = make_fake_ddgs(sleep_seconds=1.0)
        monkeypatch.setattr(web_module, "DDGS", fake)
        monkeypatch.setattr(web_module, "SEARCH_TIMEOUT_SECONDS", 0.05)
        started = time.monotonic()

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.tools.web_search"):
            result = await tool.execute(query="q")

        elapsed = time.monotonic() - started
        assert result == "搜索出错: 搜索超时(0.05秒)"
        assert elapsed < 0.9, f"超时后未及时返回, 耗时 {elapsed:.2f}s"
        assert any("超时" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_missing_dependency_gives_install_hint(self, tool, monkeypatch):
        monkeypatch.setattr(web_module, "DDGS", None)

        result = await tool.execute(query="q")

        assert result == "搜索出错: 未安装搜索依赖, 请先执行 pip install ddgs"


# ------------------------------------------------------------ 异步不阻塞事件循环


class TestAsyncBehaviour:
    @pytest.mark.asyncio
    async def test_search_runs_in_worker_thread(self, tool, monkeypatch):
        """DDGS().text() 是同步方法, 必须经 asyncio.to_thread 放到工作线程里执行."""
        fake, calls = make_fake_ddgs(results=SAMPLE_RESULTS[:1])
        monkeypatch.setattr(web_module, "DDGS", fake)

        await tool.execute(query="q")

        assert calls[0]["thread"] != threading.main_thread().name
        assert calls[0]["thread"].startswith("asyncio")  # 默认线程池的命名

    @pytest.mark.asyncio
    async def test_event_loop_stays_responsive_during_search(self, tool, monkeypatch):
        """搜索期间事件循环必须保持可调度: 心跳任务的最大间隔应远小于搜索耗时."""
        fake, _ = make_fake_ddgs(results=SAMPLE_RESULTS[:1], sleep_seconds=0.3)
        monkeypatch.setattr(web_module, "DDGS", fake)
        max_gap = 0.0
        last = time.monotonic()

        async def ticker() -> None:
            nonlocal max_gap, last
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                max_gap = max(max_gap, now - last)
                last = now

        ticker_task = asyncio.create_task(ticker())
        await tool.execute(query="q")
        ticker_task.cancel()

        # 若同步调用没被 to_thread 包住, 这里会看到约 0.3s 的调度空洞
        assert max_gap < 0.2, f"事件循环被阻塞了 {max_gap:.2f}s"

    @pytest.mark.asyncio
    async def test_new_client_instance_per_call(self, tool, monkeypatch):
        fake, calls = make_fake_ddgs()
        monkeypatch.setattr(web_module, "DDGS", fake)

        await tool.execute(query="a")
        await tool.execute(query="b")

        assert calls[0]["instance"] is not calls[1]["instance"]  # 不跨线程复用客户端


# ------------------------------------------------------------- 与 Registry 联调


class TestRegistryIntegration:
    @pytest.mark.asyncio
    async def test_registry_routes_to_web_search(self, tool, monkeypatch):
        fake, calls = make_fake_ddgs(results=SAMPLE_RESULTS[:1])
        monkeypatch.setattr(web_module, "DDGS", fake)
        registry = ToolRegistry()
        registry.register(tool)

        result = await registry.execute("web_search", {"query": "MeowMeowClaw"})

        assert registry.list_tools() == ["web_search"]
        assert calls[0]["query"] == "MeowMeowClaw"
        assert "### 1. MeowMeowClaw 项目主页" in result

    @pytest.mark.asyncio
    async def test_registry_with_empty_arguments_returns_readable_error(self, tool):
        registry = ToolRegistry()
        registry.register(tool)

        assert await registry.execute("web_search", {}) == "[错误] 搜索关键词不能为空"

    @pytest.mark.asyncio
    async def test_definition_is_exposed_to_model(self, tool):
        registry = ToolRegistry()
        registry.register(tool)

        function = registry.get_definitions()[0]["function"]
        assert function["name"] == "web_search"
        assert function["description"] == SPEC_DESCRIPTION
        assert function["parameters"]["required"] == ["query"]


# ============================================================ 真实联网连通性检测
# 说明: 这一组用例会真的访问外网, 失败通常代表**环境连通性问题**而不是代码缺陷.
# 三层探测(裸 TCP -> ddgs 库 -> WebSearchTool)是为了把问题定位到具体层次.


@pytest.mark.network
@network_only
class TestRealNetwork:
    @staticmethod
    def _tcp_probe(host: str, port: int) -> tuple[bool, str]:
        started = time.monotonic()
        try:
            socket.create_connection((host, port), timeout=TCP_PROBE_TIMEOUT).close()
            return True, f"{(time.monotonic() - started) * 1000:.0f}ms"
        except Exception as exc:  # noqa: BLE001 探测失败本身就是结论
            return False, f"{type(exc).__name__}: {exc}"

    def test_connectivity_probe(self):
        """连通性探针: 逐目标打印结果, 只要求"当前环境至少有一条外网通路".

        这样才能把"完全无外网"与"只是搜索引擎被拒/被墙"区分开.
        """
        targets = (("duckduckgo.com", 443), ("pypi.org", 443), ("1.1.1.1", 53))
        results = {}
        for host, port in targets:
            ok, detail = self._tcp_probe(host, port)
            results[f"{host}:{port}"] = ok
            print(f"  [探针] {host}:{port:<4} -> {'可达' if ok else '不可达'} ({detail})")

        assert any(results.values()), f"当前环境没有任何外网连通性: {results}"

    @pytest.mark.asyncio
    async def test_raw_ddgs_can_search(self):
        """绕过本项目代码, 直接用 ddgs 库搜索: 失败即为库/网络侧问题."""
        from ddgs import DDGS

        results = await asyncio.to_thread(
            lambda: DDGS().text("python asyncio", max_results=3)
        )

        assert isinstance(results, list) and results, f"ddgs 未返回结果: {results!r}"

    @pytest.mark.asyncio
    async def test_tool_returns_real_results(self, tool):
        """端到端: WebSearchTool 真实联网并返回格式化结果."""
        result = await tool.execute(query="python asyncio", max_results=3)

        if result.startswith("搜索出错"):
            pytest.fail(f"联网搜索失败(环境连通性问题): {result}")
        assert "### 1." in result
        assert "链接: http" in result

    @pytest.mark.asyncio
    async def test_tool_reports_error_text_instead_of_raising(self, tool):
        """无论网络通不通, 工具都必须返回文本而不能抛异常(离线环境也能验证)."""
        result = await tool.execute(query="python asyncio", max_results=1)

        assert isinstance(result, str) and result
        if result.startswith("搜索出错"):
            print(f"  [记录] 当前环境搜索不可用: {result[:160]}")
