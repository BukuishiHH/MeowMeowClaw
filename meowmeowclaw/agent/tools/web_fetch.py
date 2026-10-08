"""网页抓取工具: 下载 URL 内容并转成纯文本.

安全说明:
    - 只允许 http/https 协议;
    - 默认**拒绝**回环/内网/链路本地/保留地址(SSRF 防护): 模型可能被网页内容诱导去抓
      `http://127.0.0.1:7897`、`http://169.254.169.254/`(云元数据)、路由器后台等本机/内网资源;
    - 已知局限: `follow_redirects=True` 时, 若远端 302 跳到内网地址, 本次抓取仍会发出请求
      (只做发起前检查, 未做逐跳校验); 另存在 DNS 重绑定窗口(check 与实际连接各解析一次).
"""

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from meowmeowclaw.agent.tools import BaseTool

try:  # html2text 属于可选依赖: 缺失时不影响其它工具导入, 只在执行时给提示
    import html2text
except ImportError:  # pragma: no cover - 仅在未安装时走到
    html2text = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 15
MAX_OUTPUT_CHARS = 12000
MAX_HTML_BYTES = 2 * 1024 * 1024   # 只转换前 2MB, 避免超大页面拖垮转换(输出本来就要截断)
TRUNCATE_NOTICE = "\n...(内容过长, 已截断)"
EMPTY_NOTICE = "(未提取到正文内容)"
ALLOWED_SCHEMES = ("http", "https")

# 常见浏览器 UA, 避免被站点按爬虫处理
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.IGNORECASE)


def _is_ip_literal(host: str) -> bool:
    """host 是否本身就是 IP 字面量(含 IPv6)."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _resolve_host_ips(host: str) -> list[str]:
    """同步解析域名, 返回全部 A/AAAA 记录(由调用方丢进线程执行)."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def _is_blocked_ip(ip: Any) -> bool:
    """非"全球可达"地址一律禁止访问.

    用 ``is_global`` 统一判定, 比 ``is_private`` 更严格: 例如 100.64.0.0/10(CGNAT,
    Tailscale 等也在用)在 Python 3.11 里 ``is_private`` 为 False, 但同样不该被 Agent
    访问 -- 它覆盖回环/内网/链路本地/保留/基准测试/组播/未指定等全部情况.
    """
    return not ip.is_global


class WebFetchTool(BaseTool):
    """
    抓取指定 URL 的网页内容并转换成纯文本(Markdown)

    Args:
        allow_private: 是否允许访问回环/内网地址; 默认 False(SSRF 防护),
                       确实需要抓内网页面时才置 True

    行为约定:
        - 只接受 http/https;
        - 15 秒超时, 跟随重定向, 使用浏览器 UA;
        - HTML 用 html2text 转 Markdown(保留链接、忽略图片、不自动换行);
          非 HTML 内容(纯文本/JSON 等)原样返回;
        - 连续空行折叠为单个空行, 超过 12000 字符截断;
        - 所有异常都转成文本返回, 不抛给上层.
    """

    def __init__(self, allow_private: bool = False) -> None:
        self.allow_private = allow_private

    def __repr__(self) -> str:
        return f"<Tool name={self.name}, label={self.label}, allow_private={self.allow_private}>"

    # ------------------------------------------------------------------ 工具契约

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "抓取指定 URL 的网页内容. 当你需要阅读某个具体网页的详细内容时使用."
            "通常配合 web_search 工具先搜索再抓取."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的网页 URL, 例如 https://example.com/article",
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    # ------------------------------------------------------------------ 执行

    async def execute(self, **kwargs: Any) -> str:
        """
        抓取并转换网页

        :param kwargs: 需包含 url(str)
        :return: 纯文本正文; 被拦截/失败/异常时返回可读提示
        """
        url = str(kwargs.get("url") or "").strip()
        if not url:
            return "[错误] 抓取地址不能为空"

        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            return "安全拦截: 只允许 http/https 协议"
        if not parsed.netloc or not parsed.hostname:
            return f"[错误] URL 格式不正确: {url}"

        if html2text is None:
            return "抓取出错: 未安装 html2text 依赖, 请先执行 pip install html2text"

        if not self.allow_private:
            blocked = await self._check_private_target(parsed.hostname)
            if blocked:
                logger.warning("拦截内网抓取: %s", url)
                return blocked

        try:
            # async with 确保无论成功/异常都会关闭连接池
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
            ) as client:
                response = await client.get(url)
        except Exception as exc:  # noqa: BLE001 抓取失败不该炸主循环
            logger.warning("抓取失败: %s (%r)", url, exc)
            return f"抓取出错: {exc}"

        if not 200 <= response.status_code < 300:
            return f"抓取失败: HTTP {response.status_code}"

        return self._to_text(response)

    # ------------------------------------------------------------------ SSRF 防护

    async def _check_private_target(self, hostname: str) -> Optional[str]:
        """
        检查目标是否指向本机/内网

        :return: 命中时返回拦截提示, 否则 None
        """
        host = hostname.strip("[]")  # IPv6 字面量带方括号
        if host.lower() == "localhost" or host.lower().endswith(".localhost"):
            return f"安全拦截: 禁止访问本机/内网地址 ({hostname})"

        candidates = [host]
        if not _is_ip_literal(host):
            # 域名要解析后再判断, 否则 localtest.me 这类指向 127.0.0.1 的域名会绕过检查
            try:
                candidates = await asyncio.to_thread(_resolve_host_ips, host)
            except Exception as exc:  # noqa: BLE001 DNS 失败也是可预期的
                return f"抓取出错: 域名解析失败 ({hostname}): {exc}"

        for candidate in candidates:
            try:
                ip = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if _is_blocked_ip(ip):
                return f"安全拦截: 禁止访问本机/内网地址 ({hostname} -> {ip})"
        return None

    # ------------------------------------------------------------------ 正文转换

    def _to_text(self, response: httpx.Response) -> str:
        """响应 → 纯文本: HTML 走 html2text, 其它类型原样返回."""
        body = response.content[:MAX_HTML_BYTES]
        content_type = response.headers.get("content-type", "").lower()
        text = self._decode_body(response, body)

        looks_like_html = "html" in content_type or (
            not content_type and text.lstrip()[:1] == "<"
        )
        if looks_like_html:
            text = self._html_to_markdown(text)

        return self._clean(text)

    @staticmethod
    def _html_to_markdown(html: str) -> str:
        """按规格配置 html2text: 保留链接、忽略图片、不自动换行."""
        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.body_width = 0
        return converter.handle(html)

    @staticmethod
    def _decode_body(response: httpx.Response, body: bytes) -> str:
        """解码字节: 优先响应头声明的字符集, 缺失时嗅探 HTML 的 <meta charset>(中文站点常见)."""
        declared = response.charset_encoding
        if declared:
            try:
                return body.decode(declared, errors="replace")
            except LookupError:  # 服务端报了个不认识的字符集
                pass

        matched = _META_CHARSET_RE.search(body[:2048])
        if matched:
            encoding = matched.group(1).decode("ascii", errors="ignore")
            try:
                return body.decode(encoding, errors="replace")
            except LookupError:
                pass

        return body.decode("utf-8", errors="replace")

    @staticmethod
    def _clean(text: str) -> str:
        """折叠连续空行 → 截断 → 空内容给占位符."""
        text = re.sub(r"\n{3,}", "\n\n", text.strip())
        if not text:
            return EMPTY_NOTICE
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + TRUNCATE_NOTICE
        return text
