"""AgentCore Platform v1.0 - inner workflow Step 4: CallSlackApi (tool side-effect).

Performs the post/create/invite call against the Slack Web API via
src/services/slack_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives on
       the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner node.
       GraphNode.execute() passes the caller's InvocationContext into the inner
       subgraph UNCHANGED (no trust elevation), so a real external caller runs
       this call under its own VERIFIED_EXTERNAL context; declaring INTERNAL here
       would deny that already-gated external caller before the call ever
       runs. The node therefore stays ANONYMOUS.
  Secrets: the bot token is read via ctx.secrets.get("SLACK_BOT_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. v1 note: while the deterministic NETWORK-FREE stub transport is
       active a missing token is tolerated (a sentinel placeholder is used - it
       is never sent anywhere because no request leaves the process); with a
       LIVE transport injected, a missing token is a hard status=error - a real
       API is never called unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect against
       an external messaging system; a Slack API error (non-2xx or ok:false)
       surfaces as status=error + error_log (no silent pass).

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Slack settings (api_base_url) arrive as the JSON `slack_config` state
field - injected by the inner graph's _extra_initial_state() from the manifest
section forwarded by SlackWorkflowGraphNode._parent_config() - or via the
optional `config["configurable"]["slack"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.slack_client import SlackApiError, SlackClient

_SECRET_KEY = "SLACK_BOT_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"


class CallSlackApiNode(FunctionNode):
    """Post a message / create a channel / invite members via the Slack Web API."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: "dict[str, Any] | None" = None) -> dict[str, Any]:
        payload = from_json(state.get("slack_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallSlackApiNode: missing slack_payload"],
            }

        intent = state.get("intent", "") or ""
        method = str(payload.get("method", "") or "")
        args = payload.get("args") or {}

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["slack"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("slack_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("slack") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE v1 stub (documented limitation, docs/02).
        base_url = str(settings.get("api_base_url", "") or "").strip()
        client = SlackClient(base_url=base_url) if base_url else SlackClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # v1 stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallSlackApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        channel_name = state.get("channel_name", "") or ""

        try:
            if method == "chat.postMessage":
                channel = str(args.get("channel", "") or "")
                text = str(args.get("text", "") or "")
                if not channel:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSlackApiNode: unresolved channel - cannot post message"],
                    }
                if not text:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallSlackApiNode: no message text resolved - refusing to " "post the raw instruction"
                        ],
                    }
                resp = client.post_message(channel, text, api_token) or {}
                channel_id = str(resp.get("channel", "") or "")
                message_ts = str(resp.get("ts", "") or "")
                permalink = f"slack://channel/{channel_id}/message/{message_ts}" if channel_id and message_ts else ""
            elif method == "conversations.create":
                name = str(args.get("name", "") or "")
                if not name:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSlackApiNode: unresolved channel name - cannot create channel"],
                    }
                resp = client.create_channel(name, api_token) or {}
                channel_obj = resp.get("channel") or {}
                channel_id = str(channel_obj.get("id", "") or "")
                channel_name = str(channel_obj.get("name", "") or "") or channel_name
                message_ts = ""
                permalink = f"slack://channel/{channel_id}" if channel_id else ""
            elif method == "conversations.invite":
                channel = str(args.get("channel", "") or "")
                users = [str(u) for u in (args.get("users") or []) if str(u)]
                if not channel:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSlackApiNode: unresolved channel - cannot invite members"],
                    }
                if not users:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallSlackApiNode: no members resolved to invite"],
                    }
                resp = client.invite_members(channel, users, api_token) or {}
                channel_obj = resp.get("channel") or {}
                channel_id = str(channel_obj.get("id", "") or "")
                channel_name = str(channel_obj.get("name", "") or "") or channel_name
                message_ts = ""
                permalink = f"slack://channel/{channel_id}" if channel_id else ""
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallSlackApiNode: unknown method '{method}'"],
                }
        except SlackApiError as exc:
            # Closed-set entry: the HTTP status only. The exception's message
            # carries Slack's error field - or the whole response body when
            # there is none - and neither is text this template chose.
            status_code = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallSlackApiNode: Slack API error {status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Closed-set entry: the exception CLASS, never its message - a
            # transport error message can quote the URL it was sent to.
            failure_type = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallSlackApiNode: Slack call failed: {failure_type}"],
            }

        # Audit the tool side-effect - intent + presence signals only,
        # never message content or credentials.
        emit_trace_event(
            "call_slack_api_complete",
            {
                "intent": intent,
                "has_channel_id": bool(channel_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "message_ts": message_ts,
            "permalink": permalink,
            "status": AgentStatus.SUCCESS.value,
        }
