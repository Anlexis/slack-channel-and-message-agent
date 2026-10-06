# CMN-C2-233 - Unit tests: ValidateInputNode (inner Step 1, flag-and-redact)
# Adapted from the established tool-calling golden pattern (Kaonavi -> Slack).
#
# Canon: nodes are invoked via node(state) - through BaseNode.__call__ (trust
# gate -> input scan -> execute -> output scan) - never bare node.execute(state). This inner domain
# node declares ANONYMOUS, so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value.
#
# Input layering exercised here:
#   * the FRAMEWORK mask in __call__ rewrites emails (any '@') in
#     validated_input to "[MASKED]" BEFORE execute() sees the text - the
#     intentional-PII test asserts that [MASKED] path;
#   * the NODE's own deterministic scan handles token-shaped strings the
#     framework mask does not cover (xox*- / secret_* / sk-* / eyJ*) -
#     flag + [REDACTED].

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.validate_input_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "validated_input": "post the release note to the #general channel",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "validate-input-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestValidateInputNode:
    def setup_method(self):
        self.node = ValidateInputNode()

    def test_success_plain_text(self):
        result = self.node(_state(validated_input="announce the release in the team channel"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "announce the release in the team channel"
        assert from_json(result["redaction_flags"], None) == []

    def test_success_serialized_json_input(self):
        payload = json.dumps({"text": "post the deploy summary", "channel_hint": "release-notes"})
        result = self.node(_state(validated_input=payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "post the deploy summary"
        assert result["channel_hint"] == "release-notes"

    def test_empty_input_errors(self):
        result = self.node(_state(validated_input="  "))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_short_input_errors(self):
        result = self.node(_state(validated_input="ab"))
        assert result["status"] == AgentStatus.ERROR.value

    def test_framework_s2_masks_email_before_execute(self):
        """Intentional-PII path: the framework mask in __call__ rewrites the
        email to [MASKED] before execute() runs, so no raw address survives."""
        result = self.node(_state(validated_input="send the release summary of the deploy to hr.lead@example.com"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "hr.lead@example.com" not in result["validated_input"]
        assert "[MASKED]" in result["validated_input"]

    def test_node_s2_redacts_token_shaped_string(self):
        """The node's own deterministic scan covers token shapes the framework
        PII mask does not (secret_*): flagged + [REDACTED] before logging."""
        text = "integration key secret_abcdef123456 for the release channel post"
        result = self.node(_state(validated_input=text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "secret_abcdef123456" not in result["validated_input"]
        assert "[REDACTED]" in result["validated_input"]
        assert "token" in from_json(result["redaction_flags"], [])

    def test_node_s2_redacts_slack_bot_token_shape(self):
        """Slack's own xox[abeprs]- token family is flagged + redacted."""
        # Built at runtime so no credential-shaped literal is committed.
        leaked = "xoxb-" + "a" * 16
        result = self.node(_state(validated_input="post the note using " + leaked + " to the channel"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert leaked not in result["validated_input"]
        assert "[REDACTED]" in result["validated_input"]
        assert "token" in from_json(result["redaction_flags"], [])

    def test_audit_emits_scan_outcome_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.validate_input_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state(validated_input="post the deploy summary to the release channel"))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - flags only, never the text.
        assert payloads["validate_input_complete"]["redaction_flags"] == []
        assert "text" not in payloads["validate_input_complete"]
