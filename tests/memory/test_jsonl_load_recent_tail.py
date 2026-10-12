"""A+ A3: ``load_recent`` 反向块读 + 窗口裁剪.

对照 docs/A_PLUS_OPTIMIZATION.md §3.3/§4:

- 正常文件走尾读(打桩禁止全量 ``_read_turns``, 落到即失败);
- **差分测试**: 随机文件(含损坏行/半行/空行/中文/工具调用/超大行)上,
  尾读结果必须与"全量读取 + 相同 max_turns/max_chars 裁剪"逐消息一致;
- 字节预算不足时翻倍继续向前读, 直到满足窗口或文件头;
- 尾读异常回退全量路径; 空窗口(0)不触碰文件。
"""

import json
import random
from pathlib import Path

import pytest

import meowmeowclaw.memory.jsonl as jsonl_module
from meowmeowclaw.memory import JsonlSessionStore, SessionKey, SessionMessage

# 差分测试覆盖的窗口组合(None = 不限制; 0 = 空窗口)
WINDOWS: list[tuple[int | None, int | None]] = [
    (None, None),
    (1, None),
    (3, None),
    (None, 1),
    (None, 40),
    (2, 5),
    (4, 999_999),
    (100, 120),
    (0, 10),
    (5, 0),
]

DIFF_GROUPS = 200


def make_key(conversation_id: str) -> SessionKey:
    return SessionKey(channel="cli", scope="session", conversation_id=conversation_id)


def session_path(store: JsonlSessionStore, key: SessionKey) -> Path:
    return store.sessions_dir / f"{key.storage_id}.jsonl"


def header_line(key: SessionKey) -> str:
    return json.dumps(
        {
            "type": "header",
            "schema_version": 1,
            "storage_id": key.storage_id,
            "session_key": key.canonical,
            "created_at_ms": 1_700_000_000_000,
        },
        ensure_ascii=False,
    )


def project(messages: list[SessionMessage]) -> list[dict]:
    return [message.to_llm_message() for message in messages]


def reference_messages(
    path: Path,
    *,
    max_turns: int | None,
    max_chars: int | None,
) -> list[dict]:
    """旧全量路径的参考实现: ``_read_turns`` + 相同窗口裁剪 + 消息投影."""
    turns = JsonlSessionStore._read_turns(path)
    if max_turns is not None:
        turns = turns[-max_turns:] if max_turns > 0 else []
    if max_chars is not None:
        turns = JsonlSessionStore._trim_by_chars(turns, max_chars)
    messages: list[SessionMessage] = []
    for turn in turns:
        for raw in turn.get("messages") or []:
            try:
                messages.append(SessionMessage.from_storage_dict(raw))
            except ValueError:
                continue
    return project(messages)


def forbid_full_scan(monkeypatch) -> None:
    """正常文件不允许落到全量扫描(落到即断言失败)."""

    def _fail(cls, path):  # noqa: ANN001
        raise AssertionError(f"不应走全量扫描: {path}")

    monkeypatch.setattr(JsonlSessionStore, "_read_turns", classmethod(_fail))


async def assert_same_as_reference(
    store: JsonlSessionStore,
    key: SessionKey,
    *,
    max_turns: int | None,
    max_chars: int | None,
) -> None:
    path = session_path(store, key)
    expected = reference_messages(path, max_turns=max_turns, max_chars=max_chars)
    loaded = await store.load_recent(key, max_turns=max_turns, max_chars=max_chars)
    assert project(loaded) == expected, (
        f"窗口 max_turns={max_turns}, max_chars={max_chars} 与全量路径不一致"
    )


# ------------------------------------------------------------------ 必须走尾读


class TestTailPathIsUsed:
    @pytest.mark.asyncio
    async def test_normal_file_does_not_fall_back_to_full_scan(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-tail-used")
        for index in range(1, 5):
            await store.append_turn(key, [SessionMessage(role="user", content=f"第{index}条")])

        forbid_full_scan(monkeypatch)
        loaded = await store.load_recent(key, max_turns=2, max_chars=1000)

        assert [message.content for message in loaded] == ["第3条", "第4条"]

    @pytest.mark.asyncio
    async def test_zero_windows_do_not_touch_file(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-zero-window")
        await store.append_turn(key, [SessionMessage(role="user", content="x")])

        def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("空窗口不应读取文件")

        monkeypatch.setattr(jsonl_module, "read_last_lines", _boom)
        assert await store.load_recent(key, max_turns=0) == []
        assert await store.load_recent(key, max_chars=0) == []
        assert await store.load_recent(key, max_turns=0, max_chars=0) == []

    @pytest.mark.asyncio
    async def test_partial_last_line_does_not_force_full_scan(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-partial-tail")
        await store.append_turn(key, [SessionMessage(role="user", content="第一条")])
        with session_path(store, key).open("a", encoding="utf-8") as handle:
            handle.write('{"type":"turn","seq":2,"messages":[')  # 崩溃半行

        forbid_full_scan(monkeypatch)
        loaded = await store.load_recent(key)

        assert [message.content for message in loaded] == ["第一条"]


# ------------------------------------------------------------------ 回退与预算


class TestFallbackAndBudget:
    @pytest.mark.asyncio
    async def test_tail_read_error_falls_back_to_full_scan(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-fallback")
        for index in range(1, 4):
            await store.append_turn(key, [SessionMessage(role="user", content=f"第{index}条")])

        calls: list[Path] = []
        original = JsonlSessionStore._read_turns

        def spy(cls, path):  # noqa: ANN001
            calls.append(path)
            return original.__func__(cls, path)

        monkeypatch.setattr(JsonlSessionStore, "_read_turns", classmethod(spy))

        def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("模拟尾读失败")

        monkeypatch.setattr(jsonl_module, "read_last_lines", _boom)

        loaded = await store.load_recent(key, max_turns=2)

        assert calls, "尾读失败后必须回退全量路径"
        assert [message.content for message in loaded] == ["第2条", "第3条"]

    @pytest.mark.asyncio
    async def test_budget_is_doubled_until_window_or_file_start(self, tmp_path, monkeypatch):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-budget")
        for index in range(1, 4):
            await store.append_turn(key, [SessionMessage(role="user", content=f"第{index}条")])
        path = session_path(store, key)
        # 末尾追加一条超预算、无换行的损坏巨行: 首次读取只会拿到它, 必须翻倍继续
        with path.open("a", encoding="utf-8") as handle:
            handle.write("x" * (256 * 1024))

        budgets: list[int] = []
        real = jsonl_module.read_last_lines

        def spy(*args, **kwargs):  # noqa: ANN002, ANN003
            budgets.append(int(kwargs["max_bytes"]))
            return real(*args, **kwargs)

        monkeypatch.setattr(jsonl_module, "read_last_lines", spy)

        loaded = await store.load_recent(key, max_chars=5)

        assert len(budgets) >= 2 and budgets[1] > budgets[0]
        # 巨行损坏被跳过, 最终仍按 max_chars 窗口裁到最新一条
        assert [message.content for message in loaded] == ["第3条"]

    @pytest.mark.asyncio
    async def test_non_utf8_tail_falls_back_and_raises_like_full_path(self, tmp_path):
        """尾读遇到非法 UTF-8 时回退全量; 全量同样失败 → 与旧行为一致地抛错."""
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-bad-utf8")
        path = session_path(store, key)
        path.write_bytes(header_line(key).encode() + b"\n" + b'{"type":"turn",\xff}\n')

        with pytest.raises(UnicodeDecodeError):
            await store.load_recent(key, max_turns=1)


# ------------------------------------------------------------------ 边界场景


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_empty_and_header_only_files(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-empty")
        assert await store.load_recent(key) == []

        path = session_path(store, key)
        path.write_text("", encoding="utf-8")
        assert await store.load_recent(key) == []

        path.write_text(header_line(key) + "\n", encoding="utf-8")
        assert await store.load_recent(key) == []
        assert await store.load_recent(key, max_turns=3, max_chars=100) == []

    @pytest.mark.asyncio
    async def test_archived_session_is_empty(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-archived")
        await store.append_turn(key, [SessionMessage(role="user", content="归档内容")])
        await store.archive(key)

        assert await store.load_recent(key) == []
        assert await store.load_recent(key, max_turns=1, max_chars=100) == []

    @pytest.mark.asyncio
    async def test_corrupt_middle_and_partial_tail_are_skipped(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-corrupt")
        path = session_path(store, key)
        valid = lambda seq, text: json.dumps(  # noqa: E731
            {
                "type": "turn",
                "schema_version": 1,
                "seq": seq,
                "ts_ms": 1_700_000_000_000 + seq,
                "message_total": seq,
                "messages": [{"role": "user", "content": text}],
            },
            ensure_ascii=False,
        )
        path.write_text(
            header_line(key)
            + "\n"
            + valid(1, "第一条")
            + "\n"
            + "这不是 JSON\n"
            + "[1, 2, 3]\n"
            + valid(2, "第二条")
            + "\n"
            + '{"type":"turn","seq":3,"messages":['
            + "\n"
            + "   \n",
            encoding="utf-8",
        )

        assert [message.content for message in await store.load_recent(key)] == [
            "第一条",
            "第二条",
        ]
        await assert_same_as_reference(store, key, max_turns=1, max_chars=None)
        await assert_same_as_reference(store, key, max_turns=None, max_chars=1)

    @pytest.mark.asyncio
    async def test_valid_last_line_without_newline_is_loaded(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-no-trailing-newline")
        path = session_path(store, key)
        line = json.dumps(
            {
                "type": "turn",
                "schema_version": 1,
                "seq": 1,
                "ts_ms": 1_700_000_000_001,
                "message_total": 1,
                "messages": [{"role": "user", "content": "缺尾换行但完整"}],
            },
            ensure_ascii=False,
        )
        path.write_text(header_line(key) + "\n" + line, encoding="utf-8")

        loaded = await store.load_recent(key, max_turns=1)
        assert [message.content for message in loaded] == ["缺尾换行但完整"]

    @pytest.mark.asyncio
    async def test_utf8_multibyte_across_block_boundary(self, tmp_path):
        """单行超过一个反向块时, 跨块的中文必须完整拼回."""
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-utf8-boundary")
        path = session_path(store, key)
        rows = []
        for seq, text in ((1, "旧"), (2, "汉" * 40_000)):
            rows.append(
                json.dumps(
                    {
                        "type": "turn",
                        "schema_version": 1,
                        "seq": seq,
                        "ts_ms": 1_700_000_000_000 + seq,
                        "message_total": seq,
                        "messages": [{"role": "user", "content": text}],
                    },
                    ensure_ascii=False,
                )
            )
        path.write_text(header_line(key) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

        newest = await store.load_recent(key, max_turns=1)
        assert [message.content for message in newest] == ["汉" * 40_000]

        both = await store.load_recent(key, max_turns=2)
        assert [message.content for message in both] == ["旧", "汉" * 40_000]
        await assert_same_as_reference(store, key, max_turns=1, max_chars=10)
        await assert_same_as_reference(store, key, max_turns=None, max_chars=50_000)

    @pytest.mark.asyncio
    async def test_tail_helper_returns_chronological_suffix(self, tmp_path):
        store = JsonlSessionStore(tmp_path / "memory")
        key = make_key("a3-helper")
        for index in range(1, 6):
            await store.append_turn(key, [SessionMessage(role="user", content=f"第{index}条")])
        path = session_path(store, key)

        turns = JsonlSessionStore._read_turns_tail(path, max_turns=2, max_chars=None)

        assert [turn["seq"] for turn in turns] == [4, 5]


# ------------------------------------------------------------------ 差分测试(200 组)


def random_content(rng: random.Random) -> str | None:
    choice = rng.random()
    if choice < 0.08:
        return None
    if choice < 0.5:
        return "甲" * rng.randint(0, 300)
    if choice < 0.8:
        return "ascii-" + "x" * rng.randint(0, 300)
    return "中文" + "乙" * rng.randint(0, 800)


def random_tool_calls(rng: random.Random) -> list[dict] | None:
    if rng.random() >= 0.35:
        return None
    return [
        {
            "id": f"call_{rng.randint(0, 9)}",
            "type": "function",
            "function": {
                "name": rng.choice(["search", "搜索", "read_file"]),
                "arguments": json.dumps({"q": "汉" * rng.randint(0, 200)}, ensure_ascii=False),
            },
        }
    ]


def random_turn(rng: random.Random, seq: int) -> dict:
    messages = []
    for _ in range(rng.randint(1, 3)):
        role = rng.choice(["user", "assistant", "tool"])
        message: dict = {"role": role, "content": random_content(rng)}
        if role == "tool":
            message["tool_call_id"] = f"call_{rng.randint(0, 9)}"
        if rng.random() < 0.15:
            message["name"] = "工具" + str(rng.randint(0, 3))
        tool_calls = random_tool_calls(rng)
        if tool_calls is not None:
            message["tool_calls"] = tool_calls
        message["ts_ms"] = 1_700_000_000_000 + seq
        messages.append(message)
    return {
        "type": "turn",
        "schema_version": 1,
        "seq": seq,
        "turn_id": f"turn-{seq}",
        "ts_ms": 1_700_000_000_000 + seq,
        "message_total": seq,
        "messages": messages,
    }


def write_random_session(path: Path, key: SessionKey, rng: random.Random) -> None:
    lines = [header_line(key)]
    for seq in range(1, rng.randint(1, 12) + 1):
        if rng.random() < 0.18:
            lines.append(
                rng.choice(
                    ["这不是 JSON", "[1, 2, 3]", '{"a": ', "null", '"字符串"', "{bad json}"]
                )
            )
        lines.append(json.dumps(random_turn(rng, seq), ensure_ascii=False))
        if rng.random() < 0.12:
            lines.append("")
        if rng.random() < 0.12:
            lines.append("   ")
        if rng.random() < 0.08:
            lines.append(rng.choice(["中间损坏行", "}{", '{"type":"meta"}']))

    tail_style = rng.random()
    if tail_style < 0.15:
        text = "\n".join(lines) + "\n这不是 JSON\n"
    elif tail_style < 0.3:
        text = "\n".join(lines) + "\n[1, 2, 3]\n"
    elif tail_style < 0.45:
        text = "\n".join(lines) + "\n" + '{"type":"turn","seq":99,"messages":['
    elif tail_style < 0.6:
        text = "\n".join(lines) + "\n\n"
    elif tail_style < 0.8:
        text = "\n".join(lines)  # 末条有效 turn 无换行
    else:
        text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("seed", range(DIFF_GROUPS))
@pytest.mark.asyncio
async def test_random_differential_against_full_scan(seed: int, tmp_path):
    rng = random.Random(seed)
    store = JsonlSessionStore(tmp_path / "memory")
    key = make_key(f"a3-diff-{seed}")
    path = session_path(store, key)
    write_random_session(path, key, rng)

    for max_turns, max_chars in WINDOWS:
        await assert_same_as_reference(
            store, key, max_turns=max_turns, max_chars=max_chars
        )
