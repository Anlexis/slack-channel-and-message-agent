"""AgentCore Platform v1.0 - CMN-C2-233 Slack Channel Message Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects (and
# nested dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). slack_payload /
# slack_config / invite_users / redaction_flags are dicts/lists at the point of
# use but are stored in State as JSON strings via to_json/from_json below. Do
# NOT add credentials, secrets, or Pydantic models (PB-2 / PB-5). The Slack bot
# token is NEVER stored here - it is read via ctx.secrets in CallSlackApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Slack Channel Message agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Slack-workflow fields are added below, all NotRequired (the
    state contract). All values are JSON/msgpack-serializable primitives -
    the Slack bot token is NEVER stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target hint (channel id or #name from input_context /
    # the request envelope). Never inferred; resolution to a Slack channel is
    # explicit-only (v1: `#name` in the text, a "channel called X" phrase, a
    # "Channel:" line, or this hint - an unresolved channel stays empty).
    channel_hint: NotRequired[str]
    channel_id: NotRequired[str]  # Slack channel ID returned by the Slack Web API

    # JSON - the caller contract after PreProcessNode validated it (channel_hint
    # / message / users). Stored as a JSON string like every other container
    # field; the graph node decodes it at the inner-graph boundary.
    caller_fields: NotRequired[Optional[str]]

    # ValidateInput (deterministic input scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferSlackFields
    channel_name: NotRequired[str]  # human-readable channel name from the request
    message_text: NotRequired[str]  # message body to post (post_message intent)
    # JSON list[str] - member handles to invite (invite_member intent)
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    invite_users: NotRequired[Optional[str]]
    # JSON - assembled Slack Web API request {method, args} (stored as
    # a JSON string, not a native dict; (de)serialize via to_json/from_json).
    slack_payload: NotRequired[Optional[str]]

    # Manifest `slack:` section forwarded by _parent_config() and injected
    # by the inner graph's _extra_initial_state() (JSON string).
    slack_config: NotRequired[Optional[str]]

    # CallSlackApi
    message_ts: NotRequired[str]  # Slack message timestamp id (chat.postMessage)
    permalink: NotRequired[str]  # human-readable ref (slack://channel/<id>[/message/<ts>])

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
