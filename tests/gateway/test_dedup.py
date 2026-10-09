"""DedupCache 契约测试."""

import pytest

from meowmeowclaw.gateway import DedupCache, make_inbound


class TestDedupCache:
    def test_first_seen_false_second_true(self):
        cache = DedupCache()
        key = "qq:m-1"

        assert cache.seen(key) is False
        assert cache.seen(key) is True

    def test_empty_key_never_dedups(self):
        cache = DedupCache()

        assert cache.seen("") is False
        assert cache.seen("") is False
        assert cache.seen(None) is False
        assert len(cache) == 0

    def test_key_for_envelope(self):
        env = make_inbound(channel="qq", text="t", message_id="m-1")

        assert DedupCache.key_for(env) == "qq:m-1"
        assert DedupCache.key_for(make_inbound(channel="qq", text="t")) != ""

    def test_lru_eviction(self):
        cache = DedupCache(maxsize=2)
        assert cache.seen("a") is False
        assert cache.seen("b") is False
        assert cache.seen("c") is False  # a 被淘汰

        assert cache.seen("b") is True
        assert cache.seen("a") is False
        assert len(cache) == 2

    def test_clear(self):
        cache = DedupCache()
        cache.seen("a")
        cache.clear()

        assert cache.seen("a") is False

    def test_invalid_maxsize(self):
        with pytest.raises(ValueError):
            DedupCache(maxsize=0)
