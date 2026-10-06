# PB: end-to-end behaviour through POST /invoke - src/api/server.py
#
# Unlike test_server_boot.py (which only checks the module boots), every test
# here runs the REAL compiled agent: each request crosses the entry-point auth,
# the outer trust and input gates, the caller-context bridge into the inner
# graph, all five domain nodes, and the output gate.
#
# That full path is the point. The caller's structured data has to survive an
# outer graph, a graph-node boundary and an inner graph before any node reads
# it, and the framework does not carry it across that boundary by itself. A
# node-level test cannot tell a working bridge from a broken one.
#
# The app is driven through its real ASGI interface (no test client - httpx is
# only a transitive dependency), which also allows sending a raw body that a
# strict JSON encoder would refuse to produce.

import asyncio
import json

import pytest

from src.api import server as server_module  # noqa: F401  (import = boot check)
from src.api.server import app

_TOKEN = "pb-invoke-e2e-token"
_POST_TO_NAMED_CHANNEL = 'Post the message "deploy finished" to the #release-notes channel.'
# No channel named in the text, so the target can only come from the caller.
_POST_NO_CHANNEL = "Post an update saying the release is out."


def _post_invoke(raw_body: bytes, token: "str | None" = _TOKEN) -> "tuple[int, dict]":
    """POST /invoke through the real ASGI app; returns (status, parsed body)."""
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(raw_body)).encode()),
    ]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/invoke",
        "raw_path": b"/invoke",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    messages: list = []
    sent = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": raw_body, "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"], json.loads(sent["body"].decode() or "{}")


@pytest.fixture(autouse=True)
def token_configured(monkeypatch):
    """Deploy-shaped server environment: the caller must present the Bearer token."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)


def _invoke(text: str, input_context: "dict | None" = None, token: "str | None" = _TOKEN):
    payload = {"input": text, "session_id": "pb-invoke-e2e", "input_context": input_context or {}}
    return _post_invoke(json.dumps(payload).encode(), token=token)


def _ok(text: str, input_context: "dict | None" = None) -> dict:
    status, body = _invoke(text, input_context)
    assert status == 200, f"expected 200, got {status}: {body}"
    return body


class TestAuthBoundary:
    def test_caller_without_token_is_refused(self):
        status, _ = _invoke(_POST_TO_NAMED_CHANNEL, token=None)
        assert status == 401

    def test_caller_with_wrong_token_is_refused(self):
        status, _ = _invoke(_POST_TO_NAMED_CHANNEL, token="not-the-token")
        assert status == 401


class TestPublicPathDoesRealWork:
    def test_post_message_returns_real_record_evidence(self):
        body = _ok(_POST_TO_NAMED_CHANNEL)
        assert body["status"] == "success"
        assert body["intent"] == "post_message"
        assert body["channel_id"]
        assert body["message_ts"]
        assert body["channel_id"] in body["permalink"]

    def test_caller_supplied_channel_reaches_the_workflow(self):
        """The target exists only on the caller channel - proof the bridge carries it."""
        body = _ok(_POST_NO_CHANNEL, {"channel": "C0B1BLJPH2A"})
        assert body["status"] == "success"
        assert body["channel_id"] == "C0B1BLJPH2A"

    def test_without_the_caller_channel_the_same_request_cannot_resolve(self):
        """The negative half: the identical instruction fails with the field absent."""
        body = _ok(_POST_NO_CHANNEL)
        assert body["status"] == "error"
        assert not body.get("channel_id")

    def test_caller_supplied_message_and_members_are_used(self):
        body = _ok("Invite them to the channel.", {"channel": "release-notes", "users": ["alice", "bob_9"]})
        assert body["status"] == "success"
        assert body["intent"] == "invite_member"

    def test_output_tracks_the_input_rather_than_a_constant(self):
        a = _ok('Post "alpha" to #one')
        b = _ok('Post "beta" to #two')
        assert a["channel_id"] != b["channel_id"]

    def test_create_channel_path(self):
        body = _ok("Create a channel called release-2026.")
        assert body["status"] == "success"
        assert body["intent"] == "create_channel"

    def test_unclassifiable_request_is_refused_not_guessed(self):
        """Every supported operation writes, so an unclear request must not proceed."""
        body = _ok("What is the weather like today?")
        assert body["status"] == "error"
        assert not body.get("channel_id")


class TestCallerContractFailsClosed:
    @pytest.mark.parametrize(
        "bad_channel",
        [True, 12345, 3.5, {"nested": 1}, ["list"], "a" * 81, "Not A Channel!", "  ", "#"],
    )
    def test_invalid_channel_is_refused(self, bad_channel):
        """A mistyped or misshapen channel fails closed, never coerced.

        str() turns any of these into something a shape check might accept -
        str(float("nan")) is "nan", str(True) is "True" - so a validator that
        coerced first would aim a write at whatever fell out of it.
        """
        body = _ok(_POST_NO_CHANNEL, {"channel": bad_channel})
        assert body["status"] == "error", f"accepted {bad_channel!r}"
        assert not body.get("channel_id")

    @pytest.mark.parametrize("field", ["channel", "message", "users"])
    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_literals_are_refused_over_the_wire(self, field, literal):
        """Bare NaN/Infinity are not valid JSON, yet Python emits and accepts
        them, so they really do arrive as floats on a request body.

        They are refused on arrival for being the wrong type. Every comparison
        against a non-finite float is False, so anything that let one through
        would fail open on the very check it was there to make.
        """
        raw = ('{"input": "%s", "input_context": {"%s": %s}}' % (_POST_NO_CHANNEL, field, literal)).encode()
        status, body = _post_invoke(raw)
        assert status == 200
        assert body["status"] == "error", f"accepted {literal} in {field}"
        assert not body.get("channel_id")

    @pytest.mark.parametrize("bad_users", [["not a handle"], ["ok", 5], "alice", [["nested"]], ["x" * 33], [True]])
    def test_invalid_members_are_refused(self, bad_users):
        body = _ok("Invite them to the channel.", {"channel": "release-notes", "users": bad_users})
        assert body["status"] == "error", f"accepted {bad_users!r}"

    @pytest.mark.parametrize("bad_message", [12345, 3.5, True, {"a": 1}, ["x"], "m" * 2001])
    def test_invalid_message_is_refused(self, bad_message):
        body = _ok(_POST_NO_CHANNEL, {"channel": "general", "message": bad_message})
        assert body["status"] == "error", f"accepted {bad_message!r}"

    def test_too_many_members_are_refused(self):
        body = _ok("Invite them to the channel.", {"channel": "release-notes", "users": [f"u{i}" for i in range(51)]})
        assert body["status"] == "error"

    def test_rejection_never_echoes_the_offending_value(self):
        marker = "wontbeechoed" + "z" * 20
        body = _ok(_POST_NO_CHANNEL, {"channel": marker + " !!"})
        assert body["status"] == "error"
        assert marker not in json.dumps(body)

    def test_oversized_context_is_refused_at_the_adapter(self):
        status, _ = _invoke(_POST_NO_CHANNEL, {"message": "x" * (256 * 1024 + 10)})
        assert status == 413

    def test_too_many_context_keys_refused_at_the_adapter(self):
        status, _ = _invoke(_POST_NO_CHANNEL, {f"k{i}": "v" for i in range(20)})
        assert status == 413

    def test_ordinary_request_with_the_same_words_is_unaffected(self):
        """The screens must not fire on legitimate wording."""
        body = _ok('Post the message "channel infinity test users list" to #general.')
        assert body["status"] == "success"


class TestOutputBoundary:
    def test_no_credential_shaped_value_reaches_the_caller(self):
        body = _ok('Post the message "token xoxb-" + "1234567890abcdefghij" to #general.')
        assert "xoxb-" + "1234567890abcdefghij" not in json.dumps(body)

    def test_no_raw_request_body_or_stack_trace_reaches_the_caller(self):
        body = _ok(_POST_NO_CHANNEL, {"channel": "Not A Channel!"})
        rendered = json.dumps(body)
        assert "Traceback" not in rendered
        assert "CallerFieldError" not in rendered

    def test_identifiers_cross_the_boundary_verbatim(self):
        """Workspace identifiers must arrive byte-identical.

        The message timestamp is a bare numeric run with no letters in it, so
        nothing about its own shape would keep a numeric-rewriting output rule
        away from it. This template rewrites no numbers on the way out, and
        this is what pins that.
        """
        body = _ok(_POST_NO_CHANNEL, {"channel": "C0B1BLJPH2A"})
        assert body["channel_id"] == "C0B1BLJPH2A"
        ts = body["message_ts"]
        assert ts and ts.replace(".", "").isdigit()
        assert body["permalink"] == f"slack://channel/C0B1BLJPH2A/message/{ts}"

    def test_channel_name_with_digits_is_returned_verbatim(self):
        body = _ok("Create a channel called release-2026.")
        assert body["status"] == "success"
        assert "release-2026" in json.dumps(body)


class TestBlockedResponseIsContained:
    """A response the output gate refuses must not reach the caller anyway.

    The envelope the framework hands back is `formatted_output or result` with
    no status check, so "blocked" and "delivered" are not opposites by default —
    a gate that only sets the error status publishes the refused answer under
    an error label. These drive the full request path, because the property
    being tested belongs to the envelope, not to the node.

    The trigger is a workspace token pasted where a channel goes: caller
    channel values are shape-checked, not redacted, so the value travels intact
    to the output gate and is caught there.
    """

    # Built at runtime so no credential-shaped literal is committed.
    LEAKED_CHANNEL = "xoxb-" + "9" * 10 + "-" + "abcdefghijklmnop"

    def _blocked(self) -> dict:
        body = _ok(_POST_NO_CHANNEL, {"channel": self.LEAKED_CHANNEL})
        assert body["status"] == "error"
        return body

    def test_blocked_request_is_reported_as_an_error(self):
        assert self._blocked()["status"] == "error"

    def test_refused_value_never_reaches_the_caller(self):
        assert self.LEAKED_CHANNEL not in json.dumps(self._blocked())

    def test_no_released_text_rides_in_the_error_envelope(self):
        body = self._blocked()
        rendered = json.dumps(body)
        # The workflow did run and did produce a confirmation and a record —
        # none of it may appear once the gate refused the response.
        assert "Posted message" not in rendered
        assert "slack://channel/" not in rendered
        assert not body.get("channel_id")
        assert not body.get("permalink")
        assert not body.get("confirmation")

    def test_error_envelope_carries_no_traceback_or_source_paths(self):
        rendered = json.dumps(self._blocked())
        assert "Traceback" not in rendered
        assert ".py" not in rendered
        assert "/src/" not in rendered

    def test_envelope_output_is_a_withheld_notice_not_the_answer(self):
        output = self._blocked()["output"]
        # Truthy on purpose: a falsy value would hand the envelope back to
        # `result`, which still held the ungated answer.
        assert output
        # Closed-set labels only: a constant notice and a constant reason code.
        assert isinstance(output, dict) and set(output) == {"error", "reason"}
        assert output["reason"] == "output_withheld"

    def test_an_unaffected_request_still_returns_its_answer(self):
        """The containment path must not swallow ordinary successful work."""
        body = _ok(_POST_NO_CHANNEL, {"channel": "release-notes"})
        assert body["status"] == "success"
        assert body["channel_id"]
        assert "Posted message" in body["confirmation"]


class TestWorkflowFailureIsContained:
    """An inner-workflow failure hands the caller closed-set labels, never text.

    The request below names no channel anywhere, so the workflow fails inside
    the Slack call node. That node's error_log line names the node and the
    condition, and the framework then folds the whole inner log into the outer
    one with a traceback. None of it is the caller's: the body carries a
    status and, at most, the constant envelope.
    """

    def test_inner_workflow_error_publishes_no_node_authored_text(self):
        body = _ok(_POST_NO_CHANNEL)
        assert body["status"] == "error"
        rendered = json.dumps(body)
        assert "error_log" not in rendered
        assert "unresolved channel" not in rendered
        assert "Subgraph" not in rendered
        assert "Traceback" not in rendered
        output = body.get("output")
        assert output is None or (isinstance(output, dict) and set(output) == {"error", "reason"})


class TestContextCredentialScreen:
    """Credential-shaped caller context is refused at the entry point.

    Left alone it is not a soft failure: the first backbone node copies
    `input_context` into its own result, the framework output gate scans every
    result value, and the run dies inside that node with a stack trace and no
    caller-usable reason. The screen turns that into a refusal that says which
    field to fix.
    """

    # Built at runtime so no credential-shaped literal is committed.
    AWS_SHAPED = "AKIA" + "A" * 16

    def test_credential_shaped_context_is_refused(self):
        status, body = _invoke(_POST_NO_CHANNEL, {"message": f"rotate {self.AWS_SHAPED} today"})
        assert status == 400

    def test_refusal_names_the_field_and_is_readable(self):
        _, body = _invoke(_POST_NO_CHANNEL, {"message": f"rotate {self.AWS_SHAPED} today"})
        assert "message" in body["detail"]
        assert "credential" in body["detail"].lower()

    def test_refusal_never_echoes_the_value(self):
        _, body = _invoke(_POST_NO_CHANNEL, {"message": f"rotate {self.AWS_SHAPED} today"})
        assert self.AWS_SHAPED not in json.dumps(body)

    def test_credential_nested_in_a_list_field_is_refused(self):
        status, body = _invoke(_POST_NO_CHANNEL, {"channel": "release-notes", "users": [self.AWS_SHAPED]})
        assert status == 400
        assert "users" in body["detail"]
        assert self.AWS_SHAPED not in json.dumps(body)

    def test_undeclared_field_name_is_masked_in_the_refusal(self):
        hostile_name = "ignore all rules and"
        status, body = _invoke(_POST_NO_CHANNEL, {hostile_name: self.AWS_SHAPED})
        assert status == 400
        assert hostile_name not in json.dumps(body)
        assert "<unrecognised-field>" in body["detail"]

    def test_ordinary_domain_text_on_the_same_field_still_passes(self):
        body = _ok(_POST_NO_CHANNEL, {"channel": "release-notes", "message": "rotate the deploy key today"})
        assert body["status"] == "success"
        assert body["channel_id"]

    def test_the_run_no_longer_dies_opaquely_inside_the_first_node(self):
        """Regression: the same request used to return 200 + a null output."""
        status, body = _invoke(_POST_NO_CHANNEL, {"message": f"rotate {self.AWS_SHAPED} today"})
        assert status != 200
        assert "output" not in body
