"""DuckDuckGo 联网搜索工具.

依赖 ``ddgs``(旧包名 ``duckduckgo_search`` 也兼容): ``pip install ddgs``.
未安装时本模块依然可被导入, 只是执行时返回可读的提示文本.
"""

import asyncio
import logging
from typing import Any, Optional

from .base import BaseTool

try:  # 主用新包名 ddgs
    from ddgs import DDGS
except ImportError:  # pragma: no cover - 仅在未装新版时尝试旧包名
    try:
        from duckduckgo_search import DDGS  # type: ignore[no-redef]
    except ImportError:
        DDGS = None  # type: ignore[assignment]  # 未安装: 运行时给出提示, 不影响其它工具导入

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 20    # 上限护栏: 防止模型要 1000 条把上下文打爆
MAX_OUTPUT_CHARS = 8000
SEARCH_TIMEOUT_SECONDS = 20
TRUNCATE_NOTICE = "\n...(内容过长, 已截断)"


class WebSearchTool(BaseTool):
    """
    用 DuckDuckGo 搜索互联网并返回文本结果

    实现要点:
        - ``DDGS().text()`` 是**同步阻塞**调用, 用 ``asyncio.to_thread`` 丢进线程池执行,
          避免卡住事件循环(同时用 ``asyncio.wait_for`` 加上超时护栏);
        - 每次搜索新建一个 ``DDGS`` 实例, 避免跨线程复用同一个客户端;
        - 任何异常(含超时、网络错误、结果结构变化)都转成文本返回, 不抛给上层.
    """

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "搜索互联网获取最新信息. 当你需要查询实时信息、最新新闻或不确定的知识时使用."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"最多返回几条结果, 默认 {DEFAULT_MAX_RESULTS}",
                    "default": DEFAULT_MAX_RESULTS,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    # ------------------------------------------------------------------ 执行

    async def execute(self, **kwargs: Any) -> str:
        """
        执行联网搜索

        :param kwargs: 需包含 query(str); 可选 max_results(int)
        :return: 格式化后的搜索结果文本; 出错时返回可读提示
        """
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "[错误] 搜索关键词不能为空"

        if DDGS is None:
            return "搜索出错: 未安装搜索依赖, 请先执行 pip install ddgs"

        max_results = self._normalize_max_results(kwargs.get("max_results"))

        try:
            # 注意: 超时后线程内的同步请求无法真正中断, 只是不再等它(结果丢弃)
            results = await asyncio.wait_for(
                asyncio.to_thread(self._search, query, max_results),
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("搜索超时(%s 秒): %r", SEARCH_TIMEOUT_SECONDS, query)
            return f"搜索出错: 搜索超时({SEARCH_TIMEOUT_SECONDS}秒)"
        except Exception as exc:  # noqa: BLE001 搜索失败不该炸主循环
            logger.warning("搜索失败: %r (%r)", query, exc)
            return f"搜索出错: {exc}"

        return self._format_results(results)

    @staticmethod
    def _search(query: str, max_results: int) -> list[dict[str, Any]]:
        """同步搜索; 由 asyncio.to_thread 放到工作线程里执行."""
        return DDGS().text(query, max_results=max_results)  # type: ignore[union-attr]

    @staticmethod
    def _normalize_max_results(value: Any) -> int:
        """归一化 max_results: 非法值回退默认, 并限制在 [1, MAX_RESULTS_LIMIT]."""
        if value is None:
            return DEFAULT_MAX_RESULTS
        try:
            number = int(value)
        except (TypeError, ValueError):
            logger.warning("max_results 不是整数(%r), 回退默认值 %d", value, DEFAULT_MAX_RESULTS)
            return DEFAULT_MAX_RESULTS
        return max(1, min(number, MAX_RESULTS_LIMIT))

    @classmethod
    def _format_results(cls, results: Optional[list[Any]]) -> str:
        """把搜索结果拼成 "#序号 + 链接 + 摘要" 文本, 过长时截断."""
        if not results:
            return "未找到相关结果"

        lines: list[str] = []
        for item in results:
            if not isinstance(item, dict):  # 结构不符合预期就跳过该条
                continue
            title = str(item.get("title") or "").strip()
            href = str(item.get("href") or "").strip()
            body = str(item.get("body") or "").strip()
            lines.append(f"### {len(lines) + 1}. {title}\n链接: {href}\n{body}\n")

        if not lines:
            return "未找到相关结果"

        text = "".join(lines)
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + TRUNCATE_NOTICE
        return text
