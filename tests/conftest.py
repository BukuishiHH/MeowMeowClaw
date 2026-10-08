"""tests/ 公共 fixtures."""

import pytest


@pytest.fixture
def feed_input(monkeypatch):
    """把 input() 换成脚本化输入; 脚本用尽后抛 EOFError, 避免用例写错时死循环."""

    def _feed(*lines: str) -> list[str]:
        consumed: list[str] = []
        iterator = iter(lines)

        def fake_input(prompt: str = "") -> str:
            try:
                value = next(iterator)
            except StopIteration:
                raise EOFError
            consumed.append(value)
            return value

        monkeypatch.setattr("builtins.input", fake_input)
        return consumed

    return _feed
