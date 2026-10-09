"""meowmeowclaw/llm/tokenizer.py 的单元测试.

测试策略:
- 默认全部离线: 精确档用注入的 fake encoding / fake tokenizer, 不触发 tiktoken 下载;
- 依赖缺失/加载失败路径通过 ``sys.modules`` 注入坏模块覆盖, 断言"回退启发式 + 告警一次";
- 真实 DeepSeek tokenizer 冒烟用例仅在本地放置了 tokenizer.json 且安装了可选依赖时运行,
  否则自动 skip, 保证 CI/离线环境不红.

运行: pytest tests/test_tokenizer.py -v
"""

import logging
import math
import sys
import types

import pytest

from meowmeowclaw.llm import tokenizer as tokenizer_module
from meowmeowclaw.llm.tokenizer import (
    MESSAGE_OVERHEAD_TOKENS,
    SAFETY_FACTOR,
    HFTokenizerCounter,
    HeuristicCounter,
    TiktokenCounter,
    TokenCounter,
    build_counter,
    count_cjk,
)
from meowmeowclaw.paths import resolve_tokenizer_path

# 本地真实 tokenizer(存在才跑冒烟用例; 见设计文档 §4.7)
_REAL_TOKENIZER_PATH = resolve_tokenizer_path(None, "deepseek-flash")


@pytest.fixture(autouse=True)
def clear_warned(monkeypatch):
    """每个用例重置"只告警一次"的进程级集合, 保证告警断言可复现."""
    monkeypatch.setattr(tokenizer_module, "_WARNED", set())


# ------------------------------------------------------------------ 启发式


class TestHeuristicCounter:
    def test_empty_text(self):
        assert HeuristicCounter().count_text("") == 0

    def test_pure_cjk_is_one_token_per_char(self):
        assert HeuristicCounter().count_text("你好世界") == 4

    def test_english_uses_ceiling(self):
        # "hello world" = 11 字符 * 0.3 = 3.3 -> 4
        assert HeuristicCounter().count_text("hello world") == 4

    def test_mixed_formula(self):
        # "你好ab": 2 * 1.0 + 2 * 0.3 = 2.6 -> 3
        assert HeuristicCounter().count_text("你好ab") == 3

    def test_cjk_detection_covers_punctuation_and_ranges(self):
        assert count_cjk("你好，世界！") == 6
        assert count_cjk("hello") == 0

    def test_conservative_for_chinese(self):
        text = "帮我分析一下这个项目的上下文窗口压缩功能，看看有没有风险。"
        assert HeuristicCounter().count_text(text) >= len(text) // 2

    def test_message_overhead_and_empty_content(self):
        counter = HeuristicCounter()
        assert counter.count_message({"role": "user", "content": None}) == MESSAGE_OVERHEAD_TOKENS
        assert counter.count_message({"role": "assistant", "content": ""}) == MESSAGE_OVERHEAD_TOKENS
        assert (
            counter.count_message({"role": "user", "content": "你好"})
            == MESSAGE_OVERHEAD_TOKENS + 2
        )

    def test_tool_calls_are_counted(self):
        counter = HeuristicCounter()
        plain = counter.count_message({"role": "assistant", "content": ""})
        with_calls = counter.count_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            }
        )
        assert with_calls > plain

    def test_count_messages_sums(self):
        counter = HeuristicCounter()
        messages = [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "你好"},
        ]
        assert counter.count_messages(messages) == sum(
            counter.count_message(message) for message in messages
        )

    def test_count_tools_empty_and_non_empty(self):
        counter = HeuristicCounter()
        assert counter.count_tools(None) == 0
        assert counter.count_tools([]) == 0
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "读取工作区文件",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]
        assert counter.count_tools(tool_defs) > 0

    def test_estimate_request_applies_safety_factor(self):
        counter = HeuristicCounter()
        messages = [{"role": "user", "content": "你好"}]
        raw = counter.count_messages(messages)
        assert counter.estimate_request(messages) == math.ceil(raw * SAFETY_FACTOR)

    def test_satisfies_protocol(self):
        assert isinstance(HeuristicCounter(), TokenCounter)


# ------------------------------------------------------------------ tiktoken


class _FakeEncoding:
    name = "fake"

    def encode(self, text: str):
        return text.split()


class _BoomEncoding:
    name = "boom"

    def encode(self, text: str):
        raise ValueError("boom")


class TestTiktokenCounter:
    def test_injected_encoding(self):
        counter = TiktokenCounter(encoding=_FakeEncoding())
        assert counter.name == "tiktoken:fake"
        assert counter.count_text("a b c") == 3
        assert counter.count_text("") == 0

    def test_encode_failure_falls_back_to_heuristic(self, caplog):
        counter = TiktokenCounter(encoding=_BoomEncoding())
        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.llm.tokenizer"):
            assert counter.count_text("你好") == HeuristicCounter().count_text("你好")
        assert any("tiktoken" in record.message for record in caplog.records)

    def test_load_failure_falls_back_and_warns_once(self, monkeypatch, caplog):
        class _BoomTiktoken(types.ModuleType):
            @staticmethod
            def encoding_for_model(model):
                raise RuntimeError("offline")

        monkeypatch.setitem(sys.modules, "tiktoken", _BoomTiktoken("tiktoken"))
        counter = TiktokenCounter(model="gpt-4o")

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.llm.tokenizer"):
            assert counter.count_text("你好") == HeuristicCounter().count_text("你好")
            assert counter.count_text("你好") == 2
        assert len(caplog.records) == 1

    def test_get_encoding_name_is_used(self, monkeypatch):
        calls: list[str] = []

        class _FakeTiktoken(types.ModuleType):
            @staticmethod
            def get_encoding(name):
                calls.append(name)
                return _FakeEncoding()

        monkeypatch.setitem(sys.modules, "tiktoken", _FakeTiktoken("tiktoken"))
        counter = TiktokenCounter(model="gpt-4o", encoding_name="cl100k_base")
        assert counter.count_text("a b") == 2
        assert calls == ["cl100k_base"]
        assert counter.name == "tiktoken:fake"


# ---------------------------------------------------------------- hf tokenizer


class _FakeTokenizer:
    def encode(self, text: str):
        return types.SimpleNamespace(ids=text.split())


class TestHFTokenizerCounter:
    def test_injected_tokenizer(self, tmp_path):
        path = tmp_path / "tokenizer.json"
        path.write_text("{}", encoding="utf-8")
        counter = HFTokenizerCounter(path, tokenizer=_FakeTokenizer())
        assert counter.count_text("a b c") == 3
        assert counter.count_text("") == 0
        assert counter.name.startswith("hf:")

    def test_missing_dependency_falls_back(self, tmp_path, monkeypatch, caplog):
        # sys.modules 置 None: `from tokenizers import ...` 直接 ImportError
        monkeypatch.setitem(sys.modules, "tokenizers", None)
        path = tmp_path / "tokenizer.json"
        path.write_text("{}", encoding="utf-8")
        counter = HFTokenizerCounter(path)

        with caplog.at_level(logging.WARNING, logger="meowmeowclaw.llm.tokenizer"):
            assert counter.count_text("hello") == HeuristicCounter().count_text("hello")
        assert any("tokenizer.json" in record.message for record in caplog.records)

    @pytest.mark.skipif(_REAL_TOKENIZER_PATH is None, reason="未放置本地 tokenizer.json")
    def test_real_deepseek_tokenizer_smoke(self):
        pytest.importorskip("tokenizers")
        counter = HFTokenizerCounter(_REAL_TOKENIZER_PATH)
        tokens = counter.count_text("你好，世界 hello world")
        assert 0 < tokens < 20


# ------------------------------------------------------------------ 工厂


class TestBuildCounter:
    def test_heuristic_mode(self):
        assert isinstance(build_counter("any-model", tokenizer="heuristic"), HeuristicCounter)

    def test_auto_openai_with_tiktoken_available(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: name == "tiktoken")
        counter = build_counter("gpt-4o")
        assert isinstance(counter, TiktokenCounter)
        assert counter.name == "tiktoken:gpt-4o"

    def test_auto_openai_without_tiktoken(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: False)
        assert isinstance(build_counter("gpt-4o"), HeuristicCounter)

    def test_auto_unknown_model_falls_back(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: True)
        assert isinstance(
            build_counter("unknown-model-without-local-file"), HeuristicCounter
        )

    def test_auto_prefers_local_file_when_present(self, tmp_path, monkeypatch):
        path = tmp_path / "tokenizer.json"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: name == "tokenizers")
        counter = build_counter("my-model", hf_tokenizer_path=str(path))
        assert isinstance(counter, HFTokenizerCounter)

    def test_auto_local_file_but_no_tokenizers(self, tmp_path, monkeypatch):
        path = tmp_path / "tokenizer.json"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: False)
        assert isinstance(
            build_counter("my-model", hf_tokenizer_path=str(path)), HeuristicCounter
        )

    def test_forced_tiktoken_without_dependency(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: False)
        assert isinstance(build_counter("deepseek-chat", tokenizer="tiktoken"), HeuristicCounter)

    def test_forced_tiktoken_inline_encoding(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: name == "tiktoken")
        counter = build_counter("deepseek-chat", tokenizer="tiktoken:o200k_base")
        assert isinstance(counter, TiktokenCounter)
        assert counter.name == "tiktoken:o200k_base"

    def test_forced_hf_missing_file(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: True)
        assert isinstance(build_counter("m", tokenizer="hf"), HeuristicCounter)

    def test_invalid_mode_treated_as_auto(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "_can_import", lambda name: name == "tiktoken")
        assert isinstance(build_counter("gpt-4o", tokenizer="whatever"), TiktokenCounter)

    def test_estimate_request_available_on_returned_counter(self):
        counter = build_counter("any-model", tokenizer="heuristic")
        messages = [{"role": "user", "content": "你好"}]
        assert counter.estimate_request(messages) == math.ceil(
            counter.count_messages(messages) * SAFETY_FACTOR
        )
