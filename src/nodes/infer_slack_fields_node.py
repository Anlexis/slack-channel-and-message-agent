"""AgentCore Platform v1.0 - inner workflow Step 3: InferSlackFields.

Extracts the target channel, message text, and member handles from the
(redacted) request and assembles a validated Slack Web API request
(``{method, args}``) for the classified intent. The channel is taken only from
an explicit ``#name`` in the text, a "channel called X" phrase, a "Channel:"
line, or the caller-supplied channel_hint - an unresolved channel is left
empty rather than invented (risk mitigation: never post to / create / invite
into the wrong channel; the executor surfaces the miss as status=error).
Deterministic v1 - no LLM (docs/02_design.md "v1 Implementation Note").
"""

import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# A Slack channel ID: C/G prefix + uppercase alphanumerics (workspace-issued).
_CHANNEL_ID_SHAPE_RE = re.compile(r"^[CG][A-Z0-9]{6,20}$")
# A Slack channel name: lowercase letters, digits, dot/dash/underscore, <=80.
_CHANNEL_NAME_SHAPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
# Explicit "#channel-name" token in the request text.
_CHANNEL_IN_TEXT_RE = re.compile(r"#([a-z0-9][a-z0-9._-]{0,79})")
# "channel called/named X" phrase (create requests often use it). Curly quotes
# included so pasted rich text still matches.
_CHANNEL_CALLED_RE = re.compile(
    r"channel\s+(?:called|named)\s+[\"“#]?([a-z0-9][a-z0-9._-]{0,79})[\"”]?",
    re.IGNORECASE,
)
# Quoted message body: "..." (straight or curly quotes).
_MSG_QUOTED_RE = re.compile(r"[\"“]([^\"”\n]{1,2000})[\"”]")
# Trailing "saying ..." / "that says ..." message body.
_SAYING_RE = re.compile(r"(?:saying|that says|to say)\s+(.+)$", re.IGNORECASE)
# "@handle" member mentions (invite requests).
_USER_RE = re.compile(r"@([A-Za-z0-9._-]{1,80})")
# "Key: value" request-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs.
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Keys recognised on "Key: value" lines.
_CHANNEL_KEYS = ("channel", "channel name", "チャンネル")
_MESSAGE_KEYS = ("message", "text", "メッセージ", "本文")
_USER_KEYS = ("users", "members", "user", "member", "メンバー")


class InferSlackFieldsNode(FunctionNode):
    """Extract entities and assemble the Slack Web API request."""

    # Inner domain node - derives fields from already-validated text; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "") or ""

        # Validated caller contract, bridged in from the request. These values
        # already passed their type/shape/length bounds in the node that owns
        # the caller contract, so they are trusted ahead of anything parsed out
        # of the instruction text.
        caller = state.get("input_context") or {}
        if not isinstance(caller, dict):
            caller = {}
        channel_hint = str(caller.get("channel_hint") or state.get("channel_hint", "") or "")

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferSlackFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        channel_ref = self._resolve_channel(text, channel_hint, fields)
        message_text = str(caller.get("message") or "") or self._resolve_message(text, fields)
        users = [str(u) for u in (caller.get("users") or [])] or self._resolve_users(text, fields)

        if intent == "create_channel":
            payload = {
                "method": "conversations.create",
                "args": {"name": channel_ref, "is_private": False},
            }
        elif intent == "invite_member":
            payload = {
                "method": "conversations.invite",
                "args": {"channel": channel_ref, "users": users},
            }
        else:  # post_message
            payload = {
                "method": "chat.postMessage",
                "args": {"channel": channel_ref, "text": message_text},
            }

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_slack_fields_complete",
            {"intent": intent, "has_channel": bool(channel_ref), "n_users": len(users)},
            state,
        )

        return {
            "channel_name": channel_ref if not _CHANNEL_ID_SHAPE_RE.match(channel_ref) else "",
            "message_text": message_text,
            "invite_users": to_json(users),
            "slack_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_channel(self, text: str, channel_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit channel only: '#name' > 'channel called X' > 'Channel:' line > hint.

        Returns a channel name (no '#') or a channel ID from the hint. Never
        invented - unresolved stays empty (executor errors, not guesses).
        """
        m = _CHANNEL_IN_TEXT_RE.search(text)
        if m:
            return m.group(1)
        m = _CHANNEL_CALLED_RE.search(text)
        if m:
            return m.group(1).lower()
        for key, value in fields:
            cleaned = value.strip().lstrip("#")
            if key.strip().lower() in _CHANNEL_KEYS and _CHANNEL_NAME_SHAPE_RE.match(cleaned.lower()):
                return cleaned.lower()
        hint = channel_hint.strip().lstrip("#")
        if hint and (_CHANNEL_ID_SHAPE_RE.match(hint) or _CHANNEL_NAME_SHAPE_RE.match(hint.lower())):
            return hint if _CHANNEL_ID_SHAPE_RE.match(hint) else hint.lower()
        return ""  # unresolved - left empty, never invented

    def _resolve_message(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _MSG_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:2000]
        for key, value in fields:
            if key.strip().lower() in _MESSAGE_KEYS:
                return value.strip()[:2000]
        m = _SAYING_RE.search(text)
        if m:
            return m.group(1).strip().rstrip(".")[:2000]
        return ""

    def _resolve_users(self, text: str, fields: "list[tuple[str, str]]") -> "list[str]":
        users = _USER_RE.findall(text)
        if users:
            return users[:50]
        for key, value in fields:
            if key.strip().lower() in _USER_KEYS:
                parts = re.split(r"[,\s]+", value.strip())
                return [p.lstrip("@") for p in parts if p.lstrip("@")][:50]
        return []

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] request fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields
