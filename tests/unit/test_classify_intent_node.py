# CMN-C2-233 - Unit tests: ClassifyIntentNode (inner Step 2)
# Adapted from the established tool-calling golden pattern (Kaonavi -> Slack).
# Intents: post_message / create_channel / invite_member (deterministic
# keyword heuristic, v1 - no LLM). CMN-C2-233 is a WRITE-ONLY agent with no
# read-only default, so an unclassifiable request is a HARD status=error
# (never guess a write) - unlike a lookup-default agent.
#
# Canon: invoked via node(state), which routes the full security pipeline
# (trust gate -> input scan -> execute -> output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_post_message(self):
        result = self.node(_state('post the message "deploy complete" to the #general channel'))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "post_message"

    def test_keyword_create_channel(self):
        result = self.node(_state("create a channel called releases for the launch"))
        assert result["intent"] == "create_channel"

    def test_keyword_invite_member(self):
        result = self.node(_state("invite the new teammates to the #general channel"))
        assert result["intent"] == "invite_member"

    def test_channel_management_wins_over_post(self):
        # Priority order is channel-management first: a "create the channel and
        # post a welcome note" style request classifies as the channel operation.
        result = self.node(_state("create the channel #launch and post a welcome note"))
        assert result["intent"] == "create_channel"

    def test_no_signal_is_hard_error_never_a_guessed_write(self):
        # Write-only agent: no read-only default exists, so an unclassifiable
        # request is refused rather than guessed (docs/02 "Unknown intent").
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("refusing to guess a write" in entry for entry in result["error_log"])
        assert "intent" not in result

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state('post the message "deploy complete" to the #general channel'))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"] == {"intent": "post_message"}
