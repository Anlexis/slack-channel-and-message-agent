"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: validate the caller's request (raw NL text + the
structured caller fields) and serialize it into `validated_input` for the inner
Slack workflow graph. This node OWNS the caller-data contract: every field a
caller can supply is checked here, against explicit bounds, before any of it
reaches the workflow. Business rules (intent, entity extraction) live in the
inner graph.

Caller contract (`input_context`), every field optional:

    channel / channel_hint / channel_id   target channel: a workspace channel ID
                                          or a channel name
    message                               message body to post, free text
    users                                 member handles to invite, list of
                                          inert identifiers

Rules applied to all of them:
  - values must be the declared TYPE. A number, boolean, mapping or list where a
    string is required is refused outright, never coerced: str(float("nan"))
    is "nan", which passes a channel-name shape check, so coercion would let a
    non-finite value name the channel of a write.
  - values must match an explicit SHAPE and LENGTH bound.
  - free text is sanitized here. The framework's own input scan does not cover
    the structured caller channel, so a sanitizer that only ran on the
    instruction text would leave this channel unscreened.
  - instruction-override text (directives aimed at the MODEL: role
    reassignment, system-prompt manipulation, chat-template control tokens) is
    REFUSED, fail closed, on BOTH caller text channels — the raw instruction
    text and every decoded string in input_context, keys included, at any
    depth — before anything is carried forward. The screen is the template's
    own (src/services/security.py), never delegated to the framework gate.
  - a refusal names the FIELD and never echoes the offending value; a hostile
    field NAME is masked, never echoed either.
  - absent fields are simply absent: the workflow falls back to what it can read
    out of the instruction text.
"""

import json
import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import contains_instruction_override, redact_sensitive, sanitize_query

# A workspace-issued channel ID: C/G prefix + uppercase alphanumerics.
_CHANNEL_ID_SHAPE_RE = re.compile(r"^[CG][A-Z0-9]{6,20}$")
# A channel name: lowercase letters, digits, dot/dash/underscore, <=80.
_CHANNEL_NAME_SHAPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
# Member handles render into the confirmation, so they are locked to an inert
# identifier alphabet - free text there would be caller-controlled output.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

_MAX_MESSAGE_LEN = 2000
_MAX_USERS = 50
_CHANNEL_ALIASES = ("channel", "channel_hint", "channel_id")


class CallerFieldError(ValueError):
    """A caller-supplied field failed its contract. Carries the field name only."""


# Contract field names that may be echoed into a refusal message. Any other
# input_context key is caller-controlled text, so its spot in the reported
# path shows a placeholder — a hostile field NAME is never echoed either.
# Public because the entry-point adapter masks refusal field names by the same
# rule; one declaration keeps the two from drifting apart.
KNOWN_CONTEXT_FIELDS = frozenset({"channel", "channel_hint", "channel_id", "message", "users"})
MASKED_FIELD_NAME = "<unrecognised-field>"


def _find_instruction_override(value: object, path: str = "input_context") -> "str | None":
    """Depth-first scan of every decoded string in the mapping — keys included.

    Returns the path of the first string carrying an instruction-override
    directive, or None. The walk runs on the PARSED mapping, so JSON escaping
    cannot smuggle a phrase past it, and it covers undeclared keys too: the
    screen must hold on what the caller SENT, not only on what the contract
    keeps. Path components outside the declared contract are masked, so the
    returned path is always safe to name in an error message.
    """
    if isinstance(value, str):
        return path if contains_instruction_override(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in KNOWN_CONTEXT_FIELDS else MASKED_FIELD_NAME
            key_path = f"{path}.{safe_key}"
            if isinstance(key, str) and contains_instruction_override(key):
                return key_path
            found = _find_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _require_text(value: object, field: str, max_len: int) -> str:
    """Return a bounded string, or raise naming the field (never the value).

    Rejects every non-string type, `bool` included, so numeric input cannot be
    stringified into something that satisfies a downstream shape check.
    """
    if not isinstance(value, str):
        raise CallerFieldError(f"'{field}' must be a string")
    text = value.strip()
    if not text:
        raise CallerFieldError(f"'{field}' must not be empty")
    if len(text) > max_len:
        raise CallerFieldError(f"'{field}' exceeds the {max_len}-character limit")
    return text


def _validate_channel(raw: object, field: str) -> str:
    """Validate a channel reference: a channel ID or a channel name."""
    text = _require_text(raw, field, 80).lstrip("#")
    if _CHANNEL_ID_SHAPE_RE.match(text):
        return text
    if _CHANNEL_NAME_SHAPE_RE.match(text.lower()):
        return text.lower()
    raise CallerFieldError(f"'{field}' is not a valid channel reference")


def _validate_users(raw: object, field: str) -> "list[str]":
    """Validate the invite list: inert handles, entry-capped."""
    if not isinstance(raw, (list, tuple)):
        raise CallerFieldError(f"'{field}' must be a list")
    if len(raw) > _MAX_USERS:
        raise CallerFieldError(f"'{field}' exceeds the {_MAX_USERS}-entry limit")
    handles: list[str] = []
    for entry in raw:
        handle = _require_text(entry, field, 32).lstrip("@")
        if not _HANDLE_RE.match(handle):
            raise CallerFieldError(f"'{field}' contains an invalid member handle")
        handles.append(handle)
    return handles


def validate_caller_fields(input_context: object) -> "dict[str, object]":
    """Validate the caller contract. Raises CallerFieldError on any breach."""
    if input_context in (None, {}):
        return {}
    if not isinstance(input_context, dict):
        raise CallerFieldError("'input_context' must be a mapping")

    fields: dict[str, object] = {}

    supplied = [k for k in _CHANNEL_ALIASES if input_context.get(k) is not None]
    if supplied:
        fields["channel_hint"] = _validate_channel(input_context[supplied[0]], supplied[0])

    if input_context.get("message") is not None:
        # Screened on this channel because the framework's scan does not reach
        # it: the same strip AND the same flag-and-redact the instruction text
        # gets, or caller text would be the one unscreened way in.
        cleaned = sanitize_query(
            _require_text(input_context["message"], "message", _MAX_MESSAGE_LEN),
            max_length=_MAX_MESSAGE_LEN,
        )
        fields["message"], _ = redact_sensitive(cleaned)

    if input_context.get("users") is not None:
        fields["users"] = _validate_users(input_context["users"], "users")

    return fields


class PreProcessNode(FunctionNode):
    """Validate the caller contract and shape the request for the inner graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Slack call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ────────
        # Runs on BOTH caller text channels before anything is carried
        # forward: a refusal leaves no validated_input, no channel_hint and no
        # caller_fields for any downstream node. The screen lives in this
        # node's own execute() path — calling execute() directly still
        # refuses, so the guarantee does not depend on any framework gate
        # being present or configured on.
        if contains_instruction_override(user_input):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": "user_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request refused - instruction-override content in user_input"],
            }

        override_path = _find_instruction_override(input_context) if isinstance(input_context, (dict, list)) else None
        if override_path is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": override_path},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: request refused - instruction-override content in {override_path}"],
            }

        try:
            caller_fields = validate_caller_fields(input_context)
        except CallerFieldError as exc:
            # Fail closed, naming the field only - the rejected value is never
            # echoed into the log.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: rejected caller input - {exc}"],
            }

        # Strip control sequences + cap length before serialization.
        sanitized_input = sanitize_query(user_input.strip())

        channel_hint = str(caller_fields.get("channel_hint", ""))
        validated_input = json.dumps({"text": sanitized_input, "channel_hint": channel_hint})

        # Audit the shaped request - field presence only, never the text.
        emit_trace_event(
            "pre_process_complete",
            {
                "has_channel_hint": bool(channel_hint),
                "caller_fields": sorted(caller_fields),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "channel_hint": channel_hint,
            # Stored as a JSON string (the state contract keeps every value
            # msgpack-safe); the graph node reads it back at the boundary.
            "caller_fields": to_json(caller_fields),
            "status": AgentStatus.SUCCESS.value,
        }
