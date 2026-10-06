# CMN-C2-233 - Unit tests: CallSlackApiNode (inner Step 4, tool side-effect)
# Adapted from the established tool-calling golden pattern (Kaonavi -> Slack).
#
# Canon: invoked via node(state), which routes the full security pipeline
# (trust gate -> input scan -> execute -> output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, the trust gate unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's SlackClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_slack_api_node import CallSlackApiNode
from src.services.slack_client import SlackApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_slack_api_node.emit_trace_event", lambda *a, **k: None)


def _post_payload(channel: str = "release-announcements", text: str = "deploy complete - all checks green") -> str:
    return to_json({"method": "chat.postMessage", "args": {"channel": channel, "text": text}})


def _state(**overrides) -> dict:
    state = {
        "slack_payload": _post_payload(),
        "intent": "post_message",
        "channel_name": "release-announcements",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-slack-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for SlackClient: the call raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def post_message(self, channel, text, api_token):
        raise SlackApiError(403, "not_in_channel")


class _FakeLiveClient:
    """Stands in for SlackClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def post_message(self, channel, text, api_token):
        _FakeLiveClient.captured = {"channel": channel, "text": text, "api_token": api_token}
        return {"ok": True, "channel": "CLIVECHAN", "ts": "1234.000001"}


class TestCallSlackApiNode:
    def setup_method(self):
        self.node = CallSlackApiNode()

    def test_post_message_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_id"].startswith("C")
        assert result["message_ts"]
        assert result["permalink"] == f"slack://channel/{result['channel_id']}/message/{result['message_ts']}"
        assert result["channel_name"] == "release-announcements"

    def test_create_channel_success_via_default_v1_stub(self):
        state = _state(
            intent="create_channel",
            channel_name="releases",
            slack_payload=to_json(
                {"method": "conversations.create", "args": {"name": "releases", "is_private": False}}
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_id"].startswith("C")
        assert result["channel_name"] == "releases"
        assert result["message_ts"] == ""
        assert result["permalink"] == f"slack://channel/{result['channel_id']}"

    def test_invite_members_success_via_default_v1_stub(self):
        state = _state(
            intent="invite_member",
            channel_name="onboarding",
            slack_payload=to_json(
                {"method": "conversations.invite", "args": {"channel": "onboarding", "users": ["alice", "bob"]}}
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_id"].startswith("C")
        assert result["permalink"] == f"slack://channel/{result['channel_id']}"

    def test_slack_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `slack:` section as the JSON
        # slack_config state field; the stub transport still serves the call.
        state = _state(slack_config=to_json({"api_base_url": "https://slack.example.test/api"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_id"].startswith("C")

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"slack": {"api_base_url": "https://slack.example.test/api"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_id"].startswith("C")

    def test_missing_payload_errors(self):
        result = self.node(_state(slack_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_post_with_unresolved_channel_errors(self):
        result = self.node(_state(slack_payload=_post_payload(channel="")))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved channel" in entry for entry in result["error_log"])

    def test_post_with_no_message_text_errors(self):
        result = self.node(_state(slack_payload=_post_payload(text="")))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no message text resolved" in entry for entry in result["error_log"])

    def test_create_with_unresolved_name_errors(self):
        state = _state(
            intent="create_channel",
            slack_payload=to_json({"method": "conversations.create", "args": {"name": "", "is_private": False}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved channel name" in entry for entry in result["error_log"])

    def test_invite_with_no_users_errors(self):
        state = _state(
            intent="invite_member",
            slack_payload=to_json({"method": "conversations.invite", "args": {"channel": "onboarding", "users": []}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no members resolved" in entry for entry in result["error_log"])

    def test_unknown_method_errors(self):
        state = _state(slack_payload=to_json({"method": "chat.delete", "args": {}}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown method" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_slack_api_node.SlackClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing SLACK_BOT_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_slack_api_node.SlackClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_slack_api_node.SlackClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"SLACK_BOT_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["channel"] == "release-announcements"
        assert result["channel_id"] == "CLIVECHAN"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_slack_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_slack_api_complete"]
        assert payload["intent"] == "post_message"
        assert payload["has_channel_id"] is True
        assert payload["stub_transport"] is True
