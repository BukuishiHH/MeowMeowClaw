"""PolicyDecision / ChannelPolicy 契约测试."""

import pytest

from meowmeowclaw.gateway import (
    ACTION_AGENT,
    ACTION_IGNORE,
    ACTION_REPLY,
    ChannelPolicy,
    PolicyDecision,
    make_inbound,
    make_reply,
)

from _helpers import FixedPolicy, session_key


class TestPolicyDecision:
    def test_ignore(self):
        decision = PolicyDecision.ignore()
        assert decision.action == ACTION_IGNORE

    def test_reply_requires_at_least_one_envelope(self):
        with pytest.raises(ValueError, match="至少"):
            PolicyDecision.reply()

        request = make_inbound(channel="x", text="q")
        decision = PolicyDecision.reply(make_reply(request, "a"))
        assert decision.action == ACTION_REPLY
        assert len(decision.replies) == 1

    def test_agent_requires_session_key(self):
        with pytest.raises(ValueError, match="session_key"):
            PolicyDecision(action=ACTION_AGENT)  # type: ignore[call-arg]

        decision = PolicyDecision.agent(session_key(), "hi", meta={"channel": "fake"})
        assert decision.action == ACTION_AGENT
        assert decision.session_key is not None
        assert decision.meta == {"channel": "fake"}

    def test_invalid_action(self):
        with pytest.raises(ValueError, match="action"):
            PolicyDecision(action="magic")

    def test_meta_must_be_dict(self):
        with pytest.raises(ValueError, match="meta"):
            PolicyDecision(action=ACTION_IGNORE, meta="bad")  # type: ignore[arg-type]


class TestChannelPolicyProtocol:
    def test_fixed_policy_satisfies_protocol(self):
        policy = FixedPolicy("fake", lambda env: PolicyDecision.ignore())

        assert isinstance(policy, ChannelPolicy)
        assert policy.channel == "fake"
