# CMN-C2-233 - Unit tests: PostProcessNode (outer backbone, domain output gate)
# Adapted from the established tool-calling golden pattern (Kaonavi -> Slack).
#
# Canon: invoked via node(state), which routes the full security pipeline
# (trust gate -> input scan -> execute -> output scan); this backbone formatter
# declares ANONYMOUS -> the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value. The domain output gate is the
# MODULE-LEVEL _security_gate_output() helper (fleet-green pattern - the
# framework gate methods are @final and the real SDK auto-wraps _extra_ hooks),
# so the helper is also unit-tested directly as a plain function.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value

from src.nodes.post_process_node import (
    _ERROR_NOTICES,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
    _security_gate_output,
)
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "channel_id": "CABCDEFGH",
        "channel_name": "release-announcements",
        "message_ts": "1234.000001",
        "permalink": "slack://channel/CABCDEFGH/message/1234.000001",
        "intent": "post_message",
        "confirmation": "Posted message to channel '#release-announcements' - id=CABCDEFGH",
        "slack_payload": to_json(
            {"method": "chat.postMessage", "args": {"channel": "release-announcements", "text": "deploy complete"}}
        ),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: AgentStatus is a str-Enum, so == passes for the bare
        # enum too, and isinstance() would pass for it as well - only an exact
        # type check catches a bare-enum write. noqa is deliberate: swapping in
        # isinstance() here would silently retire the check.
        assert type(result["status"]) is str  # noqa: E721
        out = result["formatted_output"]
        assert out["channel_id"] == "CABCDEFGH"
        assert out["permalink"] == "slack://channel/CABCDEFGH/message/1234.000001"
        assert out["intent"] == "post_message"
        assert out["confirmation"].startswith("Posted message to channel")
        # Round-trip: the JSON slack_payload string surfaces parsed.
        assert out["slack_payload"]["method"] == "chat.postMessage"

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            channel_id="",
            permalink="",
            error_log=["CallSlackApiNode: Slack API error 403: not_in_channel"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Slack API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_output_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing channel_id/permalink is blocked."""
        result = self.node(_state(channel_id="", permalink=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])

    def test_audit_emits_outcome_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.post_process_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - outcome signals, no content.
        assert payloads["post_process_complete"] == {"intent": "post_message", "has_channel_id": True}


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"channel_id": "CABCDEFGH", "permalink": "slack://channel/CABCDEFGH", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"channel_id": "", "permalink": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "channel_id/permalink" in violations[0]

    def test_blocks_bearer_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"channel_id": "CABCDEFGH", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_blocks_slack_token_shaped_value(self):
        # Slack's own xox[abeprs]- bot/user token family is credential-shaped.
        leaked = "xoxb-" + "a" * 16
        violations = _security_gate_output(
            {"channel_id": "CABCDEFGH", "confirmation": "posted with " + leaked},
            is_success=True,
        )
        assert any("confirmation" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"channel_id": "", "permalink": ""}, is_success=False)
        assert violations == []


class TestOutputGateContainment:
    """A blocked response must be EMPTIED, not merely labelled failed.

    The framework composes the caller envelope as `formatted_output or result`
    and never consults status, so a gate that returns ERROR while leaving
    either field populated publishes exactly the text it refused. These pin the
    clearing itself, the truthiness of the withheld notice (a falsy stub hands
    the envelope back to `result`), and the property that makes the clearing
    survive: the delta the node returns must carry nothing the framework output
    gate would raise on.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    # Built at runtime so no credential-shaped literal is committed.
    LEAKED = "xoxb-" + "9" * 10 + "-" + "abcdefghijklmnop"

    def _blocked(self) -> dict:
        """A run whose caller-facing output carries a credential-shaped value."""
        return self.node(
            _state(
                channel_name=self.LEAKED,
                confirmation=f"Posted message to channel '#{self.LEAKED}' - id=CABCDEFGH",
            )
        )

    def test_blocked_response_is_reported_as_error(self):
        result = self._blocked()
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])

    def test_blocked_response_clears_every_output_bearing_field(self):
        result = self._blocked()
        # `result` is the field the envelope falls back to — it must be empty.
        assert not result["result"]
        for field in (
            "confirmation",
            "message_text",
            "slack_payload",
            "channel_id",
            "channel_name",
            "message_ts",
            "permalink",
            "intent",
            "validated_input",
            "caller_fields",
            "channel_hint",
            "redaction_flags",
        ):
            assert not result[field], f"{field} still carries output after a gate violation"

    def test_withheld_formatted_output_is_truthy(self):
        """`formatted_output or result` — a falsy stub re-publishes the answer."""
        result = self._blocked()
        assert result["formatted_output"], "withheld formatted_output must not be falsy"

    def test_nothing_released_survives_in_the_returned_delta(self):
        result = self._blocked()
        rendered = json.dumps(result, default=str)
        assert self.LEAKED not in rendered
        assert "Posted message" not in rendered

    def test_violation_names_the_location_not_the_value(self):
        """Load-bearing, not cosmetic.

        The framework scans this delta on the way out. A violation string that
        quoted the matched value would make that scan raise, the node wrapper
        would replace the whole return with a bare error partial, and every
        field cleared above would come back.
        """
        result = self._blocked()
        joined = "\n".join(result["error_log"])
        assert self.LEAKED not in joined
        assert "formatted_output[" in joined

    def test_delta_passes_the_framework_output_scan(self):
        """The returned delta must survive the scan that would discard it."""
        for value in self._blocked().values():
            assert detect_credentials_in_value(value) == []

    def test_withheld_path_is_audited(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.post_process_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self._blocked()
        payloads = {args[0]: args[1] for args in events}
        assert payloads["post_process_output_withheld"] == {"violation_count": 2}


class TestErrorEnvelopeIsClosedSet:
    """The caller-visible error carries closed-set labels only.

    `error_log` is node-authored free text. Wherever a node interpolates a
    caught exception it carries upstream response bodies, and identifiers,
    names and emails are one f-string away - so projecting it into
    `formatted_output` is not a bounded error contract, however much of the
    rest is cleared, truncated or redacted. Both non-success paths must hand
    the caller nothing but the constants the module declares, and a log line
    seeded into the state must appear nowhere in what the node returns.

    The workflow-failed path is driven through execute() directly: the
    framework wrapper skips execute() on an incoming errored state, and the
    outer graph routes ERROR past this node altogether, so __call__ cannot
    reach that branch. The gate-violation path runs through __call__.
    """

    PATHS = (_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD)

    # Assembled at runtime so no credential-shaped literal is committed. The
    # seeded line has the shape of a real leak - an upstream body quoting a
    # name and a token - plus a marker no legitimate value could contain.
    TOKEN = "sk-live-" + "4" * 8
    SENTINEL = "boom: upstream said {'customer':'A. Tanaka','token':'" + TOKEN + "'} SENTINEL-ERR-7f3a9c"
    FRAGMENTS = ("SENTINEL-ERR-7f3a9c", "A. Tanaka", TOKEN)

    # The gate trigger, built at runtime for the same reason.
    LEAKED = "xoxb-" + "9" * 10 + "-" + "abcdefghijklmnop"

    def setup_method(self):
        self.node = PostProcessNode()

    def _drive(self, path: str) -> dict:
        if path == _REASON_WORKFLOW_FAILED:
            # A partial record identifier is in flight on purpose: it must not
            # be published either - an id is not a closed-set label.
            state = _state(
                status=AgentStatus.ERROR.value,
                channel_id="CPARTIAL01",
                permalink="slack://channel/CPARTIAL01",
                confirmation="",
                error_log=[self.SENTINEL],
            )
            return self.node.execute(state)
        state = _state(
            channel_name=self.LEAKED,
            confirmation=f"Posted message to channel '#{self.LEAKED}' - id=CABCDEFGH",
            error_log=[self.SENTINEL],
        )
        return self.node(state)

    @pytest.mark.parametrize("path", PATHS)
    def test_error_path_is_reported_as_error(self, path):
        assert self._drive(path)["status"] == AgentStatus.ERROR.value

    @pytest.mark.parametrize("path", PATHS)
    def test_envelope_values_are_the_declared_constants(self, path):
        envelope = self._drive(path)["formatted_output"]
        assert set(envelope) == {"error", "reason"}
        assert envelope["reason"] == path
        assert envelope["error"] == _ERROR_NOTICES[path]
        declared = set(_ERROR_NOTICES) | set(_ERROR_NOTICES.values())
        assert set(envelope.values()) <= declared

    @pytest.mark.parametrize("path", PATHS)
    def test_seeded_error_text_appears_nowhere_in_the_returned_mapping(self, path):
        # json.dumps walks every nested value, so a fragment hidden inside a
        # list or a sub-mapping is found the same as a top-level one.
        rendered = json.dumps(self._drive(path), default=str)
        for fragment in self.FRAGMENTS:
            assert fragment not in rendered, f"{fragment!r} reached the returned mapping"

    @pytest.mark.parametrize("path", PATHS)
    def test_error_envelope_is_truthy(self, path):
        """`formatted_output or result` - a falsy envelope re-opens the fallback."""
        assert self._drive(path)["formatted_output"]

    def test_workflow_failure_clears_every_output_bearing_field(self):
        """The clearing is the same on every error path, not only the gate's."""
        result = self._drive(_REASON_WORKFLOW_FAILED)
        assert not result["result"]
        for field in (
            "confirmation",
            "message_text",
            "slack_payload",
            "channel_id",
            "channel_name",
            "message_ts",
            "permalink",
            "intent",
            "validated_input",
            "caller_fields",
            "channel_hint",
            "redaction_flags",
        ):
            assert not result[field], f"{field} still carries a value after a workflow failure"

    def test_workflow_failure_is_audited_with_a_count_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.post_process_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self._drive(_REASON_WORKFLOW_FAILED)
        payloads = {args[0]: args[1] for args in events}
        assert payloads["post_process_workflow_failed"] == {"error_count": 1}
