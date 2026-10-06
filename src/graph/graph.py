"""AgentCore Platform v1.0 - CMN-C2-233 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in SlackWorkflowGraphNode (`main`
slot), which wraps the inner SlackWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State, from_json

if TYPE_CHECKING:  # import cycle at runtime; the real import is inside get_subgraph()
    from src.graph.domain_workflow_graph import SlackWorkflowGraph

# Repo-root runtime config: src/graph/graph.py -> parents[2] = repo root.
# Runtime parameters live in config/config.yaml; config/agent.yaml is the static
# registry manifest and carries no runtime block.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Runtime keys forwarded to the inner graph as the `agent` section. `timeout_s`
# is the key the framework config validator reads, so it travels under that name.
_RUNTIME_KEYS = ("max_retry", "timeout_s")


def load_runtime_config() -> "dict[str, Any]":
    """Load config/config.yaml -> the dict passed to the graph as `config=`.

    The platform registry loads this file and constructs the agent with it; a
    standalone entry point must do the same, otherwise every declared runtime
    value (max_retry, timeout_s, the Slack settings) is silently absent.
    Returns {} when the file is missing or unreadable - the pipeline then runs
    on its documented defaults rather than failing to start.
    """
    try:
        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else {}


# Structured keys surfaced by get_output() on SUCCESS (docs/02 "Structured
# product surface") - read from the gated formatted_output only.
_STRUCTURED_KEYS = ("intent", "channel_id", "channel_name", "message_ts", "permalink", "confirmation")


class SlackWorkflowGraphNode(GraphNode):
    """Wraps the inner Slack workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which loads the manifest.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "SlackWorkflowGraph":
        from src.graph.domain_workflow_graph import SlackWorkflowGraph

        return SlackWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the instruction into validated_input (JSON
        # string); the first inner node parses it back.
        #
        # The validated caller contract cannot ride along in that string: the
        # framework masks validated_input at every node boundary, so caller
        # values could be rewritten between hops. It is stashed on the bridge
        # here instead - the last point that still sees the outer state before
        # the framework invokes the subgraph without forwarding input_context.
        set_caller_input_context(from_json(state.get("caller_fields"), {}))
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "channel_id": sub_result.get("channel_id", ""),
            "channel_name": sub_result.get("channel_name", ""),
            "message_ts": sub_result.get("message_ts", ""),
            "permalink": sub_result.get("permalink", ""),
            "message_text": sub_result.get("message_text", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "slack_payload": sub_result.get("slack_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Reads config/config.yaml and forwards the `slack:` integration section
        plus the runtime values (max_retry, timeout_s) as the `agent` section.
        An empty result would make every declared setting dead, so the values
        are read from the live file rather than assumed.
        """
        runtime = load_runtime_config()
        configurable: dict[str, Any] = {}
        slack = runtime.get("slack")
        if isinstance(slack, dict) and slack:
            configurable["slack"] = slack
        agent_cfg = {k: runtime[k] for k in _RUNTIME_KEYS if k in runtime}
        if agent_cfg:
            configurable["agent"] = agent_cfg
        return {"configurable": configurable}


class SlackChannelMessageAgent(AgentBaseGraph):
    """CMN-C2-233 outer graph - Slack Channel Message Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in SlackWorkflowGraphNode (`main` slot); Slack settings
    flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_233"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = SlackWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        """Structured product surface (docs/02) - EXTENDS the framework output.

        On SUCCESS - and only when the gated formatted_output carries
        record evidence (channel_id/permalink) - the structured keys are
        surfaced at the top level so callers can consume the outcome without
        parsing the confirmation string. On any non-SUCCESS status (or missing
        evidence) the structured keys are withheld: fail-closed, matching the
        PostProcess output gate.
        """
        output: dict[str, Any] = super().get_output(state)
        if state.get("status") != AgentStatus.SUCCESS.value:
            return output  # fail-closed: no structured keys on a non-SUCCESS result
        formatted = state.get("formatted_output")
        if not isinstance(formatted, dict):
            return output
        if not (formatted.get("channel_id") or formatted.get("permalink")):
            return output  # fail-closed: no record evidence -> no structured surface
        for key in _STRUCTURED_KEYS:
            output[key] = formatted.get(key, "")
        return output
