# CMN-C2-233 - Unit tests: inner SlackWorkflowGraph (BaseGraph) contract.
# Adapted from the established tool-calling golden pattern. The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free v1 stub.


from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import SlackWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return SlackWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "slack_channel_message_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_slack_config_as_json():
    g = _graph({"configurable": {"slack": {"api_base_url": "https://slack.example.test/api"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict.
    assert isinstance(extra["slack_config"], str)
    assert from_json(extra["slack_config"], {}) == {"api_base_url": "https://slack.example.test/api"}


def test_extra_initial_state_without_slack_section():
    """No slack section -> no slack_config, but the caller channel still exists."""
    extra = _graph()._extra_initial_state()
    assert "slack_config" not in extra
    assert extra["input_context"] == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {
                "channel_id": "CABCDEFGH",
                "message_ts": "1234.000001",
                "permalink": "slack://channel/CABCDEFGH/message/1234.000001",
                "confirmation": "ok",
            },
            "status": AgentStatus.SUCCESS.value,
            "intent": "post_message",
            "channel_id": "CABCDEFGH",
            "channel_name": "release-announcements",
            "message_ts": "1234.000001",
            "permalink": "slack://channel/CABCDEFGH/message/1234.000001",
            "message_text": "deploy complete",
            "confirmation": "ok",
            "slack_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "post_message"
    assert out["channel_id"] == "CABCDEFGH"
    assert out["permalink"] == "slack://channel/CABCDEFGH/message/1234.000001"
    assert out["confirmation"] == "ok"
    assert out["output"]["channel_id"] == "CABCDEFGH"


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_post_message_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"slack": {"api_base_url": "https://slack.com/api"}}})
    g.compile()
    result = g.invoke(
        user_input='post the message "deploy complete - all checks green" to the #release-announcements channel'
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["intent"] == "post_message"
    assert result["channel_id"].startswith("C")
    assert result["permalink"].startswith("slack://channel/")
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferSlackFieldsNode",
        "CallSlackApiNode",
        "ConfirmNode",
    ]
