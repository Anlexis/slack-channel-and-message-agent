"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input and runs a deterministic (regex, NOT LLM)
input scan of the inbound text for email addresses / access-token-like strings
(including Slack ``xox*-`` bot/user tokens), which are flag-and-redacted before
anything is logged. A Slack request legitimately names channels and members
(the framework PII mask in BaseNode.__call__ additionally masks
emails/phones/names in user_input / validated_input), so this is
flag-and-redact for safe logging, not a hard reject. The only deterministic
auto-reject is the empty / non-request guard.
"""

import json

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import redact_sensitive

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Validate + flag-and-redact the inbound Slack operation request."""

    # Inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        channel_hint = state.get("channel_hint", "")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                channel_hint = obj.get("channel_hint", channel_hint)
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Deterministic flag-and-redact (before any logging). The same helper
        # runs on the caller's structured fields in the pre_process node, so
        # both inbound channels are screened identically.
        redacted, flags = redact_sensitive(text)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_channel_hint": bool(channel_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "channel_hint": channel_hint,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
