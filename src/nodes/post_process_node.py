"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Slack workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods are
@final on FunctionNode and the SDK auto-wraps `_extra_` hooks (which breaks the
.invoke() chain), so domain checks live in a module-level helper invoked inline.

The gate walks the WHOLE output structure, not just its top-level string
values. `slack_payload` is a nested mapping whose `args` carry caller-derived
text, so a scan that only looked at top-level strings would step straight past
a credential sitting in the message body.

A violation must CLEAR the response, not merely mark it failed. The framework
builds the caller envelope as `formatted_output or result`, without consulting
status, so an ERROR that leaves either field populated publishes the very text
the gate just refused - the refusal becomes a label on a delivered answer. The
withheld path below therefore returns ERROR *and* blanks every field that can
carry released text or payload.

The error envelope itself is a closed set. On every non-success path the
caller receives a constant notice and a constant reason code - never a line of
`error_log`, which is node-authored text and stays internal.
"""

import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework credential scan in FunctionNode also runs on every result).
# Includes the Slack xox[abeprs]- bot/user token family.
_CREDENTIAL_LIKE_RE = re.compile(
    r"xox[abeprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}"
)


def _walk_strings(value: object, path: str) -> "list[tuple[str, str]]":
    """Yield every (path, string) in the output, however deeply it is nested.

    Mappings, sequences and bare strings are all reachable representations of a
    caller-facing value, so all three are walked.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_walk_strings(item, f"{path}['{key}']"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, f"{path}[{index}]"))
    return found


# ── Closed-set error contract ──────────────────────────────────────────────
# The caller-visible error - `formatted_output` on any non-success path -
# carries ONLY values chosen here: a fixed notice and a fixed reason code.
# Nothing read from `error_log`, from a violations list, or from any other
# node-authored string is projected into it. Those strings are free text:
# wherever a node interpolates a caught exception they carry upstream response
# bodies, and identifiers, names and emails are one f-string away. Truncating
# or redacting that text is not a closed set; not publishing it is.
# `error_log` itself is untouched - it is the internal channel the state
# reducer appends to and the audit trail reads. It is simply not the caller's.
#
# The envelope is truthy by construction. The framework composes the caller
# envelope as `formatted_output or result` without consulting status, so an
# empty value ("", {}, None) would hand the envelope straight back to `result`
# - the ungated answer - instead of suppressing it.
_REASON_WORKFLOW_FAILED = "workflow_failed"
_REASON_OUTPUT_WITHHELD = "output_withheld"
_ERROR_NOTICES: dict[str, str] = {
    _REASON_WORKFLOW_FAILED: "Request failed: the workflow did not complete.",
    _REASON_OUTPUT_WITHHELD: "Output withheld: the response did not pass the output gate.",
}


def _error_envelope(reason: str) -> "dict[str, str]":
    """The one caller-visible error shape: a notice and a reason, both constants."""
    return {"error": _ERROR_NOTICES[reason], "reason": reason}


def _contained_output_delta(reason: str) -> "dict[str, Any]":
    """State delta for every non-success return: NO released text or payload,
    and a caller-visible error drawn from the closed set above.

    Every field the workflow writes on the way to a caller-facing answer is
    listed, including the ones the current envelope does not read: an
    invariant with exceptions decays the moment a new surface starts reading
    one of the exempted fields. A fresh dict is built per call so the nested
    envelope is never shared between invocations.
    """
    return {
        # Framework output surfaces - `result` is the one get_output() falls
        # back to, and the fallback ignores status entirely.
        "result": None,
        "formatted_output": _error_envelope(reason),
        # Workflow answer text and the assembled request payload.
        "confirmation": "",
        "message_text": "",
        "slack_payload": None,
        "channel_id": "",
        "channel_name": "",
        "message_ts": "",
        "permalink": "",
        "intent": "",
        # Caller-supplied text that travelled alongside the answer.
        "validated_input": "",
        "caller_fields": None,
        "channel_hint": "",
        "redaction_flags": None,
    }


def _security_gate_output(formatted_output: dict[str, Any], is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no record evidence (channel_id/permalink), which
        would misrepresent the Slack action outcome to the caller;
      - any credential-shaped string ANYWHERE in the caller-facing output,
        including inside nested payload mappings and lists.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("channel_id") or formatted_output.get("permalink")):
        problems.append("PostProcess output gate: SUCCESS output missing channel_id/permalink evidence")
    for path, text in _walk_strings(formatted_output, "formatted_output"):
        if _CREDENTIAL_LIKE_RE.search(text):
            # Name the location, never the matched value.
            problems.append(f"PostProcess output gate: credential-like value in {path}")
    return problems


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        errored = state.get("status") == AgentStatus.ERROR.value

        # The inner workflow failed: keep the error status (never mask it) and
        # hand the caller the closed-set envelope. error_log already carries
        # the inner entries - the state reducer appends, so re-emitting them
        # here would duplicate every line - and it is not the caller's
        # channel. Nothing in flight (a partial record identifier, answer
        # text, the request payload) is published either: the same clearing
        # applies on every error path. The audit trail gets a count, not text.
        if errored:
            emit_trace_event(
                "post_process_workflow_failed",
                {"error_count": len(state.get("error_log", []))},
                state,
            )
            return {
                **_contained_output_delta(_REASON_WORKFLOW_FAILED),
                "status": AgentStatus.ERROR.value,
            }

        formatted_output = {
            "channel_id": state.get("channel_id", ""),
            "channel_name": state.get("channel_name", ""),
            "message_ts": state.get("message_ts", ""),
            "permalink": state.get("permalink", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "slack_payload": from_json(state.get("slack_payload"), {}),
        }

        # Domain output gate (module-level helper - see module docstring).
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # Containment: fail the response AND empty it. Returning ERROR on
            # its own leaves `result` holding the inner answer, which the
            # envelope then publishes under the error status.
            #
            # The violation strings name the LOCATION only, never the matched
            # value, and that is load-bearing rather than tidy: this delta is
            # itself scanned by the framework output gate, so a quoted
            # credential would make that gate raise, the node wrapper would
            # replace this return with a bare error partial, and every field
            # cleared below would come back.
            emit_trace_event(
                "post_process_output_withheld",
                {"violation_count": len(violations)},
                state,
            )
            return {
                **_contained_output_delta(_REASON_OUTPUT_WITHHELD),
                "status": AgentStatus.ERROR.value,
                "error_log": violations,
            }

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_channel_id": bool(state.get("channel_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
