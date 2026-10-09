"""模型 token 计数: 精确档(可选依赖) + CJK 加权启发式兜底.

设计见 ``docs/CONTEXT_COMPRESSION_DESIGN.md`` §4, 三档:

- ``TiktokenCounter``: OpenAI 系列模型, 依赖可选包 ``tiktoken``;
- ``HFTokenizerCounter``: 本地 ``tokenizer.json``, 依赖可选包 ``tokenizers``;
- ``HeuristicCounter``: 零依赖, 永远可用, 默认兜底.

约定:
- 所有精确计数 **lazy 加载**: 第一次计数时才打开编码/文件; 加载失败(未装依赖/离线/
  缓存缺失/文件损坏)只告警一次, 该实例永久回退启发式, 不重试、不阻塞请求;
- 计数口径与压缩预算一致: 消息固定开销 + content + tool_calls + name/tool_call_id,
  工具定义按 JSON 序列化整体计数(见设计文档 §4.4);
- 本模块只做"数 token", 不含任何压缩/裁剪策略.
"""

import importlib
import json
import logging
import math
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from meowmeowclaw.paths import resolve_tokenizer_path

logger = logging.getLogger(__name__)

# 每条消息固定开销(role / 分隔符等), 见设计文档 §4.4
MESSAGE_OVERHEAD_TOKENS = 4
# 估算值乘该系数后再与预算比较, 见设计文档 §4.5
SAFETY_FACTOR = 1.1
# 启发式系数: CJK 按 1.0 token/字符(保守上界), 其他按 0.3 token/字符
CJK_TOKENS_PER_CHAR = 1.0
OTHER_TOKENS_PER_CHAR = 0.3

_CJK_PATTERN = re.compile(
    r"[\u2e80-\u2eff\u3000-\u303f\u3300-\u33ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)
# auto 模式下可尝试 tiktoken 的模型名前缀
_TIKTOKEN_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt")

# 每进程只提醒一次的降级信息(避免刷屏)
_WARNED: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def _can_import(module_name: str) -> bool:
    """真实尝试 import; 找不到/依赖缺失都返回 False(比 find_spec 更可靠)."""
    try:
        importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 可选依赖缺失的原因很多, 统一降级
        return False
    return True


def count_cjk(text: str) -> int:
    """统计 CJK 字符数(含中文标点/兼容区)."""
    return len(_CJK_PATTERN.findall(text))


@runtime_checkable
class TokenCounter(Protocol):
    """token 计数器契约(结构化类型, 便于替换/单测)."""

    name: str

    def count_text(self, text: str) -> int:
        """纯文本 token 数(空串为 0)."""
        ...

    def count_message(self, message: Mapping[str, Any]) -> int:
        """单条 OpenAI 消息(含固定开销 / content / tool_calls / name)."""
        ...

    def count_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        """消息列表总 token 数."""
        ...

    def count_tools(self, tool_defs: Optional[Sequence[Mapping[str, Any]]]) -> int:
        """工具定义(function schema)总 token 数; 空/None 为 0."""
        ...

    def estimate_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_defs: Optional[Sequence[Mapping[str, Any]]] = None,
        *,
        safety_factor: float = SAFETY_FACTOR,
    ) -> int:
        """完整请求估算 = ceil((messages + tools) * safety_factor)."""
        ...


class BaseTokenCounter(ABC):
    """共享消息/工具计数逻辑; 子类只需实现 ``count_text``."""

    name: str = "base"

    @abstractmethod
    def count_text(self, text: str) -> int:
        """纯文本 token 数."""

    # ------------------------------------------------------------ 共享实现

    def count_message(self, message: Mapping[str, Any]) -> int:
        total = MESSAGE_OVERHEAD_TOKENS

        content = message.get("content")
        if isinstance(content, str):
            total += self.count_text(content)
        elif content is not None:
            # 非字符串(多模态/异常结构): 用启发式对序列化结果兜底, 不走精确编码
            total += _HEURISTIC.count_text(
                json.dumps(content, ensure_ascii=False, default=str)
            )

        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += self.count_text(
                json.dumps(tool_calls, ensure_ascii=False, default=str)
            )

        for key in ("name", "tool_call_id"):
            value = message.get(key)
            if isinstance(value, str) and value:
                total += self.count_text(value)
        return total

    def count_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        return sum(self.count_message(message) for message in messages)

    def count_tools(self, tool_defs: Optional[Sequence[Mapping[str, Any]]]) -> int:
        if not tool_defs:
            return 0
        return self.count_text(
            json.dumps(list(tool_defs), ensure_ascii=False, default=str)
        )

    def estimate_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_defs: Optional[Sequence[Mapping[str, Any]]] = None,
        *,
        safety_factor: float = SAFETY_FACTOR,
    ) -> int:
        raw = self.count_messages(messages) + self.count_tools(tool_defs)
        return math.ceil(raw * safety_factor)


class HeuristicCounter(BaseTokenCounter):
    """零依赖启发式: ``ceil(CJK * 1.0 + 其他 * 0.3)``.

    中文保守上界、英文略高估; 误差方向偏安全(宁可早压缩, 不可漏判超窗).
    """

    name = "heuristic-cjk"

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        cjk = count_cjk(text)
        other = len(text) - cjk
        return math.ceil(cjk * CJK_TOKENS_PER_CHAR + other * OTHER_TOKENS_PER_CHAR)


_HEURISTIC = HeuristicCounter()


class TiktokenCounter(BaseTokenCounter):
    """OpenAI 系列精确计数(可选依赖 ``tiktoken``).

    首次计数时才加载 encoding; 加载/编码失败时告警一次并永久回退启发式.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        encoding_name: Optional[str] = None,
        encoding: Any = None,
    ) -> None:
        self._model = model
        self._encoding_name = encoding_name
        self._encoding = encoding
        self._failed = False
        label = encoding_name or model or "?"
        self.name = f"tiktoken:{label}"
        if encoding is not None:
            self.name = f"tiktoken:{getattr(encoding, 'name', 'injected')}"

    def _ensure_encoding(self) -> Any:
        if self._encoding is not None or self._failed:
            return self._encoding
        try:
            import tiktoken  # type: ignore[import-not-found]  # 可选依赖

            if self._encoding_name:
                self._encoding = tiktoken.get_encoding(self._encoding_name)
            else:
                self._encoding = tiktoken.encoding_for_model(self._model)
            self.name = f"tiktoken:{getattr(self._encoding, 'name', self._encoding_name or self._model)}"
        except Exception as exc:  # noqa: BLE001 可选依赖/网络/缓存问题统一降级
            self._failed = True
            _warn_once(
                "tiktoken-load",
                "tiktoken 加载失败(%s: %s), 该计数器回退启发式: model=%r encoding=%r",
                type(exc).__name__,
                exc,
                self._model,
                self._encoding_name,
            )
        return self._encoding

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        encoding = self._ensure_encoding()
        if encoding is None:
            return _HEURISTIC.count_text(text)
        try:
            return len(encoding.encode(text))
        except Exception as exc:  # noqa: BLE001 编码失败同样降级
            self._failed = True
            _warn_once(
                "tiktoken-encode",
                "tiktoken 编码失败(%s: %s), 该计数器回退启发式",
                type(exc).__name__,
                exc,
            )
            return _HEURISTIC.count_text(text)


class HFTokenizerCounter(BaseTokenCounter):
    """本地 ``tokenizer.json`` 精确计数(可选依赖 ``tokenizers``).

    文件读取/编码失败时告警一次并永久回退启发式; 完全离线可用.
    """

    def __init__(self, path: Path, *, tokenizer: Any = None) -> None:
        self.path = Path(path)
        self._tokenizer = tokenizer
        self._failed = False
        self.name = f"hf:{self.path.parent.name}/{self.path.name}"
        if tokenizer is not None:
            self.name = f"hf:{self.path.parent.name}/{self.path.name}(injected)"

    def _ensure_tokenizer(self) -> Any:
        if self._tokenizer is not None or self._failed:
            return self._tokenizer
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]  # 可选依赖

            self._tokenizer = Tokenizer.from_file(str(self.path))
        except Exception as exc:  # noqa: BLE001 可选依赖/文件损坏统一降级
            self._failed = True
            _warn_once(
                "hf-load",
                "tokenizer.json 加载失败(%s: %s), 该计数器回退启发式: %s",
                type(exc).__name__,
                exc,
                self.path,
            )
        return self._tokenizer

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        tokenizer = self._ensure_tokenizer()
        if tokenizer is None:
            return _HEURISTIC.count_text(text)
        try:
            return len(tokenizer.encode(text).ids)
        except Exception as exc:  # noqa: BLE001 编码失败同样降级
            self._failed = True
            _warn_once(
                "hf-encode",
                "tokenizer.json 编码失败(%s: %s), 该计数器回退启发式: %s",
                type(exc).__name__,
                exc,
                self.path,
            )
            return _HEURISTIC.count_text(text)


def _parse_tokenizer_setting(value: str) -> tuple[str, Optional[str]]:
    """解析 ``tokenizer`` 配置: 返回 (mode, inline_encoding).

    支持 ``auto`` / ``heuristic`` / ``tiktoken`` / ``tiktoken:<encoding>`` / ``hf``;
    非法值返回 ``("", None)``, 由调用方按 auto 处理.
    """
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered.startswith("tiktoken:"):
        encoding = text.split(":", 1)[1].strip()
        return "tiktoken", encoding or None
    if lowered in ("auto", "heuristic", "tiktoken", "hf"):
        return lowered, None
    return "", None


def _is_tiktoken_model(model: Optional[str]) -> bool:
    name = str(model or "").strip().lower()
    return any(name.startswith(prefix) for prefix in _TIKTOKEN_MODEL_PREFIXES)


def build_counter(
    model: Optional[str],
    *,
    tokenizer: str = "auto",
    hf_tokenizer_path: Optional[str] = None,
    tiktoken_encoding: Optional[str] = None,
) -> BaseTokenCounter:
    """按配置和模型名选择计数器(设计文档 §4.2 的解析顺序).

    - ``auto``: OpenAI 系模型优先 tiktoken; 否则尝试本地 tokenizer.json; 都不行则启发式;
    - 强制模式依赖/文件缺失时 warning 并回退启发式, 不抛异常、不阻塞启动.

    :param model: 主模型名(用于 tiktoken 映射与 tokenizer.json 候选路径)
    :param tokenizer: ``auto`` / ``heuristic`` / ``tiktoken[:encoding]`` / ``hf``
    :param hf_tokenizer_path: 显式 tokenizer.json 路径; 空则按模型名查默认目录
    :param tiktoken_encoding: 显式 encoding(优先级低于 ``tiktoken:<encoding>``)
    """
    mode, inline_encoding = _parse_tokenizer_setting(tokenizer)
    encoding_name = inline_encoding or (tiktoken_encoding or None)
    raw_path = str(hf_tokenizer_path).strip() if hf_tokenizer_path else ""

    if mode == "":
        _warn_once(
            "tokenizer-mode",
            "tokenizer 取值不合法(%r), 按 auto 处理",
            tokenizer,
        )
        mode = "auto"

    if mode == "heuristic":
        return HeuristicCounter()

    if mode == "tiktoken":
        if not _can_import("tiktoken"):
            _warn_once(
                "no-tiktoken",
                "未安装可选依赖 tiktoken, 回退启发式计数(安装: pip install tiktoken)",
            )
            return HeuristicCounter()
        return TiktokenCounter(model=model, encoding_name=encoding_name)

    if mode == "hf":
        path = resolve_tokenizer_path(raw_path or None, model or "")
        if path is None:
            _warn_once(
                "no-hf-file",
                "未找到 tokenizer.json(配置=%r, model=%r), 回退启发式计数",
                raw_path or None,
                model,
            )
            return HeuristicCounter()
        if not _can_import("tokenizers"):
            _warn_once(
                "no-tokenizers",
                "未安装可选依赖 tokenizers, 回退启发式计数(安装: pip install tokenizers)",
            )
            return HeuristicCounter()
        return HFTokenizerCounter(path)

    # auto
    if _is_tiktoken_model(model) and _can_import("tiktoken"):
        return TiktokenCounter(model=model, encoding_name=encoding_name)

    path = resolve_tokenizer_path(raw_path or None, model or "")
    if path is not None:
        if _can_import("tokenizers"):
            return HFTokenizerCounter(path)
        _warn_once(
            "no-tokenizers",
            "发现本地 tokenizer 文件 %s 但未安装可选依赖 tokenizers, 回退启发式计数",
            path,
        )
        return HeuristicCounter()

    if raw_path:
        _warn_once(
            "hf-missing",
            "配置的 hf_tokenizer_path 不存在或不是文件: %r, 回退启发式计数",
            raw_path,
        )
    else:
        _warn_once(
            "no-exact-tokenizer",
            "模型 %r 无可用精确分词器(未安装 tiktoken / 未放置 tokenizer.json), 回退启发式计数",
            model,
        )
    return HeuristicCounter()
