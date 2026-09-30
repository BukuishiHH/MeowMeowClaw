"""backend/agent/tools/web_fetch.py 的 Mock 单元测试.

测试策略:
- 用 **真实 httpx.AsyncClient + MockTransport** 替换网络层: 走的是 httpx 真正的
  重定向/超时/头部逻辑, 只是不发真实请求, 因此能验证 "async with 是否真的关了连接",
  比整体 mock 掉 AsyncClient 保真得多;
- DNS 解析默认打桩成公网 IP(autouse), 单测零网络依赖; 内外网判定用例按需覆盖;
- 另有一组 `RUN_NETWORK_TESTS=1` 才跑的真实联网用例.

运行: pytest backend/test/test_web_fetch.py -v
"""

import asyncio
import os
import re
from typing import Any, Optional
from unittest.mock import MagicMock

import httpx
import pytest

import backend.agent.tools.web_fetch as fetch_module
from backend.agent.tools import BaseTool, WebFetchTool
from backend.agent.tools.registry import ToolRegistry
from backend.agent.tools.web_fetch import (
    EMPTY_NOTICE,
    MAX_OUTPUT_CHARS,
    TRUNCATE_NOTICE,
    USER_AGENT,
)

PUBLIC_IP = "93.184.216.34"  # 任意公网地址, 仅用于打桩解析结果
SPEC_DESCRIPTION = (
    "抓取指定 URL 的网页内容. 当你需要阅读某个具体网页的详细内容时使用."
    "通常配合 web_search 工具先搜索再抓取."
)

HTML_PAGE = """<html><head><title>标题</title></head><body>
<h1>大标题</h1>
<p>正文第一段, 带一个<a href="https://example.com/next">链接</a>. </p>
<p><img src="https://example.com/a.png" alt="图"></p>
<p>正文第二段. </p>
</body></html>"""


# ---------------------------------------------------------------- 联网测试开关

RUN_NETWORK_TESTS = os.getenv("RUN_NETWORK_TESTS") == "1"
network_only = pytest.mark.skipif(
    not RUN_NETWORK_TESTS,
    reason="真实联网用例: 需外网, 设 RUN_NETWORK_TESTS=1 开启",
)


# --------------------------------------------------------------------- 测试替身


def install_mock_transport(monkeypatch, handler) -> dict:
    """把 httpx.AsyncClient 换成"真实客户端 + MockTransport", 记录构造参数与实例."""
    real_client_cls = httpx.AsyncClient  # 必须提前保存, 否则工厂里会递归调用自己
    record: dict[str, Any] = {}

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        record["kwargs"] = kwargs
        client = real_client_cls(transport=httpx.MockTransport(handler), **kwargs)
        record["client"] = client
        return client

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", factory)
    return record


def html_response(body: str = HTML_PAGE, status: int = 200, content_type: str = "text/html") -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": content_type}, content=body.encode("utf-8")
    )


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch):
    """默认所有域名都解析成公网 IP, 保证单测不依赖真实 DNS."""
    monkeypatch.setattr(fetch_module, "_resolve_host_ips", lambda host: [PUBLIC_IP])


@pytest.fixture
def tool() -> WebFetchTool:
    return WebFetchTool()


@pytest.fixture
def fetched(monkeypatch):
    """返回 (tool, record); handler 可后续替换."""

    def _install(handler) -> tuple[WebFetchTool, dict]:
        record = install_mock_transport(monkeypatch, handler)
        return WebFetchTool(), record

    return _install


# ------------------------------------------------------------------ 工具契约


class TestToolContract:
    def test_spec_constants(self):
        """规格写死的数值必须钉字面量."""
        assert fetch_module.FETCH_TIMEOUT_SECONDS == 15
        assert MAX_OUTPUT_CHARS == 12000
        assert TRUNCATE_NOTICE == "\n...(内容过长, 已截断)"
        assert fetch_module.ALLOWED_SCHEMES == ("http", "https")

    def test_is_base_tool_with_expected_name(self, tool):
        assert isinstance(tool, BaseTool)
        assert tool.name == "web_fetch"
        assert tool.label == tool.name

    def test_description_matches_spec_verbatim(self, tool):
        assert tool.description == SPEC_DESCRIPTION

    def test_parameters_schema(self, tool):
        schema = tool.parameters

        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["url"]
        assert schema["properties"]["url"]["type"] == "string"

    def test_to_function_definition_is_llm_ready(self, tool):
        definition = tool.to_function_definition()

        assert definition["function"]["name"] == "web_fetch"
        assert definition["function"]["parameters"] == tool.parameters

    def test_repr_shows_private_flag(self):
        assert "allow_private=False" in repr(WebFetchTool())
        assert "allow_private=True" in repr(WebFetchTool(allow_private=True))


# ---------------------------------------------------------- URL 安全检查(协议)


class TestUrlSafety:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com",
            "javascript:alert(1)",
            "data:text/html,<b>x</b>",
            "ssh://root@example.com",
            "//example.com/x",  # 缺 scheme
            "/etc/passwd",
        ],
    )
    @pytest.mark.asyncio
    async def test_non_http_scheme_is_blocked(self, tool, url, monkeypatch):
        record = install_mock_transport(monkeypatch, lambda request: html_response())

        result = await tool.execute(url=url)

        assert result in {"安全拦截: 只允许 http/https 协议", f"[错误] URL 格式不正确: {url}"}
        assert "client" not in record  # 根本没发请求

    @pytest.mark.parametrize("kwargs", [{}, {"url": ""}, {"url": "   "}, {"url": None}])
    @pytest.mark.asyncio
    async def test_empty_url_is_rejected(self, tool, kwargs, monkeypatch):
        record = install_mock_transport(monkeypatch, lambda request: html_response())

        assert await tool.execute(**kwargs) == "[错误] 抓取地址不能为空"
        assert "client" not in record

    @pytest.mark.parametrize("url", ["http://", "https://", "http:///only-path", "https:///x"])
    @pytest.mark.asyncio
    async def test_malformed_url_reports_error(self, tool, url, monkeypatch):
        record = install_mock_transport(monkeypatch, lambda request: html_response())

        assert await tool.execute(url=url) == f"[错误] URL 格式不正确: {url}"
        assert "client" not in record

    @pytest.mark.asyncio
    async def test_scheme_check_is_case_insensitive(self, fetched):
        tool, record = fetched(lambda request: html_response())

        result = await tool.execute(url="HTTPS://EXAMPLE.COM/")

        assert result.startswith("# 大标题")   # 大写 scheme/host 正常放行
        assert "client" in record


# ------------------------------------------------------ SSRF 防护(内网/回环)


class TestSsrfProtection:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:7897/",
            "http://127.1.2.3/",
            "http://[::1]/",
            "http://localhost/",
            "http://LocalHost:8080/admin",
            "http://foo.localhost/",
            "http://192.168.1.1/",
            "http://10.0.0.5/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",  # 云元数据
            "http://0.0.0.0/",
            "http://100.64.0.1/",  # CGNAT
        ],
    )
    @pytest.mark.asyncio
    async def test_private_targets_are_blocked(self, tool, url, monkeypatch):
        record = install_mock_transport(monkeypatch, lambda request: html_response())

        result = await tool.execute(url=url)

        assert result.startswith("安全拦截: 禁止访问本机/内网地址"), result
        assert "client" not in record  # 拦截时绝不发请求

    @pytest.mark.asyncio
    async def test_domain_resolving_to_loopback_is_blocked(self, tool, monkeypatch):
        monkeypatch.setattr(fetch_module, "_resolve_host_ips", lambda host: ["127.0.0.1"])
        record = install_mock_transport(monkeypatch, lambda request: html_response())

        result = await tool.execute(url="http://localtest.me/")

        assert result == "安全拦截: 禁止访问本机/内网地址 (localtest.me -> 127.0.0.1)"
        assert "client" not in record

    @pytest.mark.asyncio
    async def test_domain_with_any_private_record_is_blocked(self, tool, monkeypatch):
        monkeypatch.setattr(
            fetch_module, "_resolve_host_ips", lambda host: [PUBLIC_IP, "192.168.0.9"]
        )
        install_mock_transport(monkeypatch, lambda request: html_response())

        assert "安全拦截" in await tool.execute(url="http://mixed.example.com/")

    @pytest.mark.asyncio
    async def test_public_target_passes_check(self, tool, monkeypatch):
        record = install_mock_transport(monkeypatch, lambda request: html_response())

        await tool.execute(url="https://example.com/")

        assert record["kwargs"]["follow_redirects"] is True  # 确实走到发请求

    @pytest.mark.asyncio
    async def test_allow_private_opt_in(self, monkeypatch):
        record = install_mock_transport(monkeypatch, lambda request: html_response())
        tool = WebFetchTool(allow_private=True)

        result = await tool.execute(url="http://127.0.0.1:8000/health")

        assert "安全拦截" not in result
        assert "client" in record  # 放开后真的发了请求

    @pytest.mark.asyncio
    async def test_dns_failure_reports_error(self, tool, monkeypatch):
        def boom(host: str) -> list[str]:
            raise OSError("Name or service not known")

        monkeypatch.setattr(fetch_module, "_resolve_host_ips", boom)
        install_mock_transport(monkeypatch, lambda request: html_response())

        result = await tool.execute(url="http://no-such-host-xyz.example/")

        assert result == "抓取出错: 域名解析失败 (no-such-host-xyz.example): Name or service not known"


# ------------------------------------------------------------ 请求与响应处理


class TestFetchAndConvert:
    @pytest.mark.asyncio
    async def test_html_is_converted_to_markdown(self, fetched):
        tool, _ = fetched(lambda request: html_response())

        result = await tool.execute(url="https://example.com/article")

        assert "# 大标题" in result                      # 标题层级保留
        assert "正文第一段" in result
        assert "[链接](https://example.com/next)" in result  # ignore_links=False
        assert "a.png" not in result and "![" not in result  # ignore_images=True

    @pytest.mark.parametrize(
        "paragraph",
        [
            # 必须用"带空格的 ASCII 长句": textwrap 只在空格处折行,
            # 中文长句即使 body_width=78 也不会被折断, 区分不出这个配置项
            "The quick brown fox jumps over the lazy dog. " * 8,
            "这是一段很长的话" * 30,  # 顺带覆盖中文不被破坏
        ],
    )
    @pytest.mark.asyncio
    async def test_long_paragraph_is_not_wrapped(self, fetched, paragraph):
        """body_width=0: 长段落必须保持整行, 不能被按 78 列折断."""
        tool, _ = fetched(
            lambda request: html_response(f"<html><body><p>{paragraph}</p></body></html>")
        )

        result = await tool.execute(url="https://example.com/long")

        assert paragraph.strip() in result  # 若被折行插入换行, 这里会失配

    @pytest.mark.asyncio
    async def test_request_options(self, fetched):
        tool, record = fetched(lambda request: html_response())

        await tool.execute(url="https://example.com/")

        assert record["kwargs"]["timeout"] == 15
        assert record["kwargs"]["follow_redirects"] is True

    @pytest.mark.asyncio
    async def test_browser_user_agent_is_sent(self, fetched):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers.get("user-agent", "")
            return html_response()

        tool, _ = fetched(handler)
        await tool.execute(url="https://example.com/")

        assert seen["ua"] == USER_AGENT
        assert "Mozilla/5.0" in seen["ua"] and "Chrome/" in seen["ua"]

    @pytest.mark.asyncio
    async def test_client_is_closed_after_call(self, fetched):
        """规格要求必须用 async with: 调用结束后连接池应已关闭."""
        tool, record = fetched(lambda request: html_response())

        await tool.execute(url="https://example.com/")

        assert record["client"].is_closed is True

    @pytest.mark.asyncio
    async def test_client_is_closed_even_on_failure(self, fetched):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        tool, record = fetched(handler)

        await tool.execute(url="https://example.com/")

        assert record["client"].is_closed is True

    @pytest.mark.parametrize("status", [301, 400, 403, 404, 500, 502])
    @pytest.mark.asyncio
    async def test_non_2xx_returns_status_text(self, fetched, status):
        tool, _ = fetched(lambda request: httpx.Response(status, content=b"x"))

        assert await tool.execute(url="https://example.com/") == f"抓取失败: HTTP {status}"

    @pytest.mark.asyncio
    async def test_redirects_are_followed(self, fetched):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/old":
                return httpx.Response(302, headers={"location": "https://example.com/new"})
            return html_response("<html><body><p>新地址的内容</p></body></html>")

        tool, _ = fetched(handler)

        assert "新地址的内容" in await tool.execute(url="https://example.com/old")

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("连接被拒绝"),
            httpx.ReadTimeout("读取超时"),
            httpx.TooManyRedirects("重定向过多"),
            OSError("底层网络故障"),
        ],
    )
    @pytest.mark.asyncio
    async def test_network_exceptions_are_wrapped(self, fetched, exc):
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc

        tool, _ = fetched(handler)

        result = await tool.execute(url="https://example.com/")

        assert result.startswith("抓取出错: ")

    @pytest.mark.asyncio
    async def test_non_html_content_is_returned_as_is(self, fetched):
        tool, _ = fetched(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"name": "MeowMeowClaw", "stars": 1}',
            )
        )

        result = await tool.execute(url="https://api.example.com/info")

        assert result == '{"name": "MeowMeowClaw", "stars": 1}'

    @pytest.mark.asyncio
    async def test_charset_from_header(self, fetched):
        body = "<html><body><p>中文编码测试</p></body></html>".encode("gbk")
        tool, _ = fetched(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/html; charset=gbk"}, content=body
            )
        )

        assert "中文编码测试" in await tool.execute(url="https://example.com/gbk")

    @pytest.mark.asyncio
    async def test_charset_sniffed_from_meta_tag(self, fetched):
        body = '<html><head><meta charset="gbk"></head><body><p>元标签编码</p></body></html>'.encode("gbk")
        tool, _ = fetched(
            lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=body)
        )

        assert "元标签编码" in await tool.execute(url="https://example.com/meta-gbk")

    @pytest.mark.asyncio
    async def test_consecutive_blank_lines_are_collapsed(self, fetched):
        tool, _ = fetched(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"a\n\n\n\n\nb\n\n\n\n"
            )
        )

        assert await tool.execute(url="https://example.com/x") == "a\n\nb"

    @pytest.mark.asyncio
    async def test_long_content_is_truncated(self, fetched):
        huge = "<html><body><p>" + "a" * (MAX_OUTPUT_CHARS + 3000) + "</p></body></html>"
        tool, _ = fetched(lambda request: html_response(huge))

        result = await tool.execute(url="https://example.com/big")

        assert len(result) == MAX_OUTPUT_CHARS + len(TRUNCATE_NOTICE)
        assert result.endswith(TRUNCATE_NOTICE)

    @pytest.mark.asyncio
    async def test_exactly_at_limit_is_kept(self, fetched):
        body = "b" * MAX_OUTPUT_CHARS
        tool, _ = fetched(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/plain"}, content=body.encode()
            )
        )

        result = await tool.execute(url="https://example.com/edge")

        assert len(result) == MAX_OUTPUT_CHARS
        assert TRUNCATE_NOTICE not in result

    @pytest.mark.asyncio
    async def test_empty_body_returns_placeholder(self, fetched):
        tool, _ = fetched(
            lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"")
        )

        assert await tool.execute(url="https://example.com/empty") == EMPTY_NOTICE

    @pytest.mark.asyncio
    async def test_missing_html2text_dependency(self, tool, monkeypatch):
        monkeypatch.setattr(fetch_module, "html2text", None)
        record = install_mock_transport(monkeypatch, lambda request: html_response())

        result = await tool.execute(url="https://example.com/")

        assert result == "抓取出错: 未安装 html2text 依赖, 请先执行 pip install html2text"
        assert "client" not in record  # 缺依赖时不发请求


# ------------------------------------------------------------- 与 Registry 联调


class TestRegistryIntegration:
    @pytest.mark.asyncio
    async def test_registry_routes_url_argument(self, fetched):
        tool, _ = fetched(lambda request: html_response())
        registry = ToolRegistry()
        registry.register(tool)

        result = await registry.execute("web_fetch", {"url": "https://example.com/"})

        assert registry.list_tools() == ["web_fetch"]
        assert "# 大标题" in result

    @pytest.mark.asyncio
    async def test_registry_with_empty_arguments(self, tool):
        registry = ToolRegistry()
        registry.register(tool)

        assert await registry.execute("web_fetch", {}) == "[错误] 抓取地址不能为空"

    @pytest.mark.asyncio
    async def test_definition_is_exposed_to_model(self, tool):
        registry = ToolRegistry()
        registry.register(tool)

        function = registry.get_definitions()[0]["function"]
        assert function["name"] == "web_fetch"
        assert function["description"] == SPEC_DESCRIPTION
        assert function["parameters"]["required"] == ["url"]


# ------------------------------------------------------------ 真实联网(可选)


@pytest.mark.network
@network_only
class TestRealNetwork:
    @pytest.mark.asyncio
    async def test_fetch_real_page(self):
        """真实抓取 example.com, 验证 HTTP/转换/清理整条链路."""
        tool = WebFetchTool()

        result = await tool.execute(url="https://example.com/")

        if result.startswith("抓取出错"):
            pytest.fail(f"联网抓取失败(环境连通性问题): {result}")

        # 注意: example.com 的文案由 IANA 维护且会变, 这里只断言"稳定特征"
        assert result.strip(), "抓取结果不应为空"
        assert "domain" in result.lower(), result
        assert "http" in result, "页面里的链接应被保留(ignore_links=False)"

    @pytest.mark.asyncio
    async def test_real_site_boundary_is_still_enforced(self):
        """联网状态下也必须拦住内网目标(防护不能被网络条件影响)."""
        tool = WebFetchTool()

        assert (await tool.execute(url="http://127.0.0.1:7897/")).startswith("安全拦截")
