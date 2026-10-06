# CMN-C2-233 - Unit tests: InferSlackFieldsNode (inner Step 3)
# Adapted from the established tool-calling golden pattern (Kaonavi -> Slack).
#
# Canon: invoked via node(state), which routes the full security pipeline
# (trust gate -> input scan -> execute -> output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework mask rewrites Title-Case
# bigrams / '@' / digit groups in validated_input to "[MASKED]" before
# execute() sees the text - so member handles in positive invite tests travel
# on a "Members:" line (no '@'), and channel-ID-shaped hints travel in the
# channel_hint state field (not a scanned field).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_slack_fields_node import InferSlackFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_slack_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "post_message", channel_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "channel_hint": channel_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferSlackFieldsNode:
    def setup_method(self):
        self.node = InferSlackFieldsNode()

    def test_post_extracts_channel_and_quoted_message(self):
        result = self.node(
            _state('post the message "deploy complete - all checks green" to the #release-announcements channel')
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_name"] == "release-announcements"
        assert result["message_text"] == "deploy complete - all checks green"
        # slack_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["slack_payload"], str)
        assert from_json(result["slack_payload"], {}) == {
            "method": "chat.postMessage",
            "args": {"channel": "release-announcements", "text": "deploy complete - all checks green"},
        }

    def test_post_message_from_saying_tail(self):
        result = self.node(_state("send a note to the #general channel saying the deploy is done"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["message_text"] == "the deploy is done"
        payload = from_json(result["slack_payload"], {})
        assert payload["args"]["channel"] == "general"

    def test_create_channel_called_phrase(self):
        result = self.node(_state("create a channel called releases for the launch", intent="create_channel"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["slack_payload"], {})
        assert payload == {"method": "conversations.create", "args": {"name": "releases", "is_private": False}}
        assert result["channel_name"] == "releases"

    def test_channel_from_key_value_line(self):
        text = "post the deploy summary\nChannel: release-notes\nMessage: the deploy is done"
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["slack_payload"], {})
        assert payload["args"]["channel"] == "release-notes"
        assert payload["args"]["text"] == "the deploy is done"

    def test_channel_id_shaped_hint_used_when_text_has_no_channel(self):
        # The ID-shaped hint travels in channel_hint (not a scanned field);
        # an ID-shaped channel is not a human-readable channel_name.
        result = self.node(_state("post the deploy summary saying all checks green", channel_hint="CABCDEFGH"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_name"] == ""
        payload = from_json(result["slack_payload"], {})
        assert payload["args"]["channel"] == "CABCDEFGH"

    def test_explicit_channel_in_text_wins_over_hint(self):
        result = self.node(_state("post the summary to #general saying done", channel_hint="release-notes"))
        payload = from_json(result["slack_payload"], {})
        assert payload["args"]["channel"] == "general"

    def test_invite_users_from_members_line(self):
        text = "invite the new teammates to the #onboarding channel\nMembers: alice, bob"
        result = self.node(_state(text, intent="invite_member"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["invite_users"], []) == ["alice", "bob"]
        payload = from_json(result["slack_payload"], {})
        assert payload == {
            "method": "conversations.invite",
            "args": {"channel": "onboarding", "users": ["alice", "bob"]},
        }

    def test_unresolved_channel_left_empty_never_invented(self):
        result = self.node(_state("post the deploy summary saying all checks green"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_name"] == ""
        payload = from_json(result["slack_payload"], {})
        assert payload["args"]["channel"] == ""

    def test_non_shape_conforming_hint_left_unresolved(self):
        result = self.node(_state("post the deploy summary saying done", channel_hint="Not A Valid Channel!"))
        payload = from_json(result["slack_payload"], {})
        assert payload["args"]["channel"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_field_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_slack_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state('post the message "deploy complete" to the #general channel'))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals, never content.
        payload = payloads["infer_slack_fields_complete"]
        assert payload["intent"] == "post_message"
        assert payload["has_channel"] is True
        assert payload["n_users"] == 0
