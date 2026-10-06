"""AgentCore Platform v1.0 - inner Slack workflow graph (Cat 2 domain workflow).

Instantiated by SlackWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_slack_fields
          -> call_slack_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    slack    - integration section (api_base_url, ...) from config/config.yaml;
               injected into State as the JSON `slack_config` field via
               _extra_initial_state() so the no-arg nodes can read it
    agent    - runtime values from config/config.yaml (max_retry, timeout_s)

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_slack_fields_node import InferSlackFieldsNode
from src.nodes.call_slack_api_node import CallSlackApiNode
from src.nodes.confirm_node import ConfirmNode
from src.graph.context_bridge import get_caller_input_context
from src.schemas.state import State, to_json


class SlackWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "slack_channel_message_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the slack section is optional (the client falls
        # back to the documented default api_base_url + the network-free v1
        # stub transport), and a missing/unusable setting is handled at
        # CallSlackApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_slack_fields"] = InferSlackFieldsNode()
        self._nodes["call_slack_api"] = CallSlackApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_slack_fields")
        self._sg.add_edge("infer_slack_fields", "call_slack_api")
        self._sg.add_edge("call_slack_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: AgentState) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        # Forward the `slack` section (arriving under config["configurable"]
        # from _parent_config()) into State as a JSON string (msgpack-safe) so
        # the no-arg CallSlackApiNode can read it via state.get("slack_config").
        extra: dict[str, Any] = {}
        configurable = self.config.get("configurable") or {}
        slack = configurable.get("slack") or {}
        if slack:
            extra["slack_config"] = to_json(slack)
        # The framework does not forward input_context into a subgraph, so the
        # validated caller contract is read back off the bridge here - this hook
        # runs inside subgraph.invoke(), after the outer state is out of reach.
        extra["input_context"] = get_caller_input_context()
        return extra

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "channel_id": state.get("channel_id", ""),
            "channel_name": state.get("channel_name", ""),
            "message_ts": state.get("message_ts", ""),
            "permalink": state.get("permalink", ""),
            "message_text": state.get("message_text", ""),
            "confirmation": state.get("confirmation", ""),
            "slack_payload": state.get("slack_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
