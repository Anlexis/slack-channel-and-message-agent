# CMN-C2-233 - Unit tests: manifest + runtime-config sanity, and the proof that
# the declared runtime values actually reach the graph that consumes them.
#
# config/agent.yaml is the flat registry manifest (identity + compile-time
# gates, all keys at root level). config/config.yaml holds the runtime
# parameters. A reader pointed at the wrong file returns nothing and every
# declared value goes quietly dead, so the propagation tests below assert the
# values arrive rather than just that they are declared.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_CONFIG_DIR = pathlib.Path(__file__).parents[2] / "config"
_MANIFEST_PATH = _CONFIG_DIR / "agent.yaml"
_RUNTIME_PATH = _CONFIG_DIR / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


# -- manifest (config/agent.yaml) ------------------------------------------


def test_manifest_identity():
    data = _manifest()
    assert data["id"] == "CMN-C2-233"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["namespace"] == "cmn"
    assert data["enabled"] is True


def test_manifest_keys_are_flat():
    """The registry reads every key at root level - no nested `agent:` block."""
    data = _manifest()
    assert "agent" not in data
    assert "config" not in data


def test_manifest_entry_point():
    """A single dotted import path, not split module:/class: keys."""
    assert _manifest()["class"] == "src.graph.graph.SlackChannelMessageAgent"


def test_manifest_security():
    data = _manifest()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate
    # (VERIFIED_EXTERNAL); inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_no_unprovisioned_requirement():
    """`requires` is derived from what the code actually demands.

    The bot token is read with an optional lookup and the shipped transport runs
    without one, so nothing is declared here; declaring a requirement the
    deployment does not provision fails the agent at compile time instead.
    """
    requires = _manifest()["requires"]
    assert requires["secrets"] == []
    assert requires["extras"] == []


def test_manifest_generation_mode_matches_the_code():
    """No model is invoked anywhere in the pipeline."""
    assert _manifest()["generation_mode"] == "deterministic"


# -- runtime config (config/config.yaml) -----------------------------------


def test_runtime_config_values():
    runtime = _runtime()
    assert isinstance(runtime["max_retry"], int)
    # `timeout_s` is the name the framework config validator reads.
    assert isinstance(runtime["timeout_s"], int)
    assert runtime["slack"]["api_base_url"] == "https://slack.com/api"


def test_runtime_config_has_no_retired_key_names():
    """A stale key name is silently ignored, so pin the live spelling."""
    runtime = _runtime()
    assert "timeout_seconds" not in runtime


# -- propagation: the declared values reach their consumers ----------------


def test_loader_reads_the_runtime_file():
    from src.graph.graph import load_runtime_config

    assert load_runtime_config() == _runtime()


def test_runtime_values_reach_the_inner_graph():
    """The forwarded config must carry the real file values, never an empty dict."""
    from src.graph.graph import SlackWorkflowGraphNode

    configurable = SlackWorkflowGraphNode()._parent_config()["configurable"]
    runtime = _runtime()
    assert configurable["slack"] == runtime["slack"]
    assert configurable["agent"]["max_retry"] == runtime["max_retry"]
    assert configurable["agent"]["timeout_s"] == runtime["timeout_s"]


def test_slack_settings_reach_inner_state():
    """End of the chain: the settings arrive as the inner graph's state field."""
    from src.graph.domain_workflow_graph import SlackWorkflowGraph
    from src.graph.graph import SlackWorkflowGraphNode
    from src.schemas.state import from_json

    node_config = SlackWorkflowGraphNode()._parent_config()
    extra = SlackWorkflowGraph(config=node_config)._extra_initial_state()
    assert from_json(extra["slack_config"], {}) == _runtime()["slack"]


def test_outer_graph_receives_runtime_config():
    """The framework reads max_retry off the graph's own config."""
    from src.graph.graph import SlackChannelMessageAgent, load_runtime_config

    agent = SlackChannelMessageAgent(config=load_runtime_config())
    assert agent.config["max_retry"] == _runtime()["max_retry"]
    agent.compile()  # the framework validates max_retry/timeout_s here
