"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the posted / created / invited Slack outcome (channel id + reference +
name) into a human-readable confirmation message, surfacing the affected
channel/message for human review (risk mitigation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VERBS = {
    "post_message": "Posted message to channel",
    "create_channel": "Created channel",
    "invite_member": "Invited members to channel",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-produced data -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        channel_id = state.get("channel_id", "")
        channel_name = state.get("channel_name", "")
        message_ts = state.get("message_ts", "")
        permalink = state.get("permalink", "")
        intent = state.get("intent", "")

        if not channel_id and not permalink:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no channel_id/permalink to confirm"],
            }

        verb = _VERBS.get(intent, "Processed Slack operation for channel")
        label = f"#{channel_name}" if channel_name else channel_id
        parts = [f"{verb} '{label}'"]
        if permalink:
            parts.append(f"ref={permalink}")
        if channel_id:
            parts.append(f"id={channel_id}")
        if message_ts:
            parts.append(f"ts={message_ts}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_permalink": bool(permalink)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "channel_id": channel_id,
                "message_ts": message_ts,
                "permalink": permalink,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
