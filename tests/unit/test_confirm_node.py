# CMN-C2-233 - Unit tests: ConfirmNode (inner Step 5)
# Adapted from the established tool-calling golden pattern (Kaonavi -> Slack).
#
# Canon: invoked via node(state), which routes the full security pipeline
# (trust gate -> input scan -> execute -> output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "channel_id": "CABCDEFGH",
        "channel_name": "release-announcements",
        "message_ts": "1234.000001",
        "permalink": "slack://channel/CABCDEFGH/message/1234.000001",
        "intent": "post_message",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_post_message_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Posted message to channel" in result["confirmation"]
        assert "'#release-announcements'" in result["confirmation"]
        assert "ref=slack://channel/CABCDEFGH/message/1234.000001" in result["confirmation"]
        assert "id=CABCDEFGH" in result["confirmation"]
        assert "ts=1234.000001" in result["confirmation"]
        assert result["result"]["channel_id"] == "CABCDEFGH"
        assert result["result"]["permalink"] == "slack://channel/CABCDEFGH/message/1234.000001"
        assert result["result"]["confirmation"] == result["confirmation"]

    def test_create_channel_verb(self):
        result = self.node(
            _state(
                intent="create_channel", channel_name="releases", message_ts="", permalink="slack://channel/CABCDEFGH"
            )
        )
        assert "Created channel" in result["confirmation"]
        assert "ts=" not in result["confirmation"]

    def test_invite_member_verb(self):
        result = self.node(_state(intent="invite_member"))
        assert "Invited members to channel" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed Slack operation for channel" in result["confirmation"]

    def test_id_label_when_channel_name_missing(self):
        result = self.node(_state(channel_name=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "'CABCDEFGH'" in result["confirmation"]

    def test_id_only_no_permalink(self):
        result = self.node(_state(permalink=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=CABCDEFGH" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(channel_id="", permalink=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_and_reference_presence_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.confirm_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - never the confirmation text.
        assert payloads["confirm_complete"] == {"intent": "post_message", "has_permalink": True}
