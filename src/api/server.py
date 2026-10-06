"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from shared.secrets import factory as secrets_factory
from src.graph.graph import SlackChannelMessageAgent, load_runtime_config
from src.nodes.pre_process_node import KNOWN_CONTEXT_FIELDS, MASKED_FIELD_NAME

app = FastAPI(title="Agent")

# Runtime values (max_retry, timeout_s, the Slack settings) come from
# config/config.yaml. Constructing the graph without them would leave every
# declared setting unread, so the standalone entry point loads them the same
# way the platform registry does.
agent = SlackChannelMessageAgent(config=load_runtime_config())
agent.compile()
# namespace/agent_name match the manifest's `namespace` and `name` values.
agent.provision_secrets(secrets_factory(namespace="cmn", agent_name="SlackChannelMessageAgent"))

# Caller-supplied structured data travels in `input_context` alongside the
# free-text instruction. Bounds are enforced in the pipeline's first node,
# which owns the caller contract; the adapter only caps the envelope so a
# hostile body cannot make the request itself expensive to parse.
_MAX_CONTEXT_KEYS = 16
_MAX_CONTEXT_BYTES = 256 * 1024


def credential_bearing_field(input_context: "dict[str, Any]") -> "str | None":
    """Name the first context field holding a credential-shaped value, or None.

    Why this runs at the entry point rather than in the pipeline: the first
    backbone node copies `input_context` verbatim into its own result, and the
    framework output gate scans every value of every node result. A
    credential-shaped string anywhere in the context therefore aborts the run
    inside that first node, with a stack trace in the error log and nothing the
    caller can act on — before any validation this agent owns has run. The
    request cannot succeed either way, so the useful thing to do is refuse it
    where the reason is still legible.

    The scan is `detect_credentials_in_value` — the same function that gate
    calls, already recursing mappings and sequences — so what is refused here
    and what is blocked there are the same set by construction, with no local
    approximation to drift out of step.

    Only the field NAME is returned, and only when it belongs to the declared
    caller contract; anything else reports the mask, since an undeclared key is
    caller-controlled text that must not be echoed back either.
    """
    for name, value in input_context.items():
        if detect_credentials_in_value(value):
            return name if name in KNOWN_CONTEXT_FIELDS else MASKED_FIELD_NAME
    return None


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    input_context: dict[str, Any] = {}


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> "dict[str, Any]":
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary — a deployment-level caller
    # credential, not an agent secret, so ctx.secrets does not apply (no
    # InvocationContext exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    if len(req.input_context) > _MAX_CONTEXT_KEYS:
        raise HTTPException(status_code=413, detail="input_context has too many keys.")
    if len(json.dumps(req.input_context, default=str)) > _MAX_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context is too large.")
    offending_field = credential_bearing_field(req.input_context)
    if offending_field is not None:
        # Names the field, never the value — the refusal must not become a
        # second copy of the credential.
        raise HTTPException(
            status_code=400,
            detail=(
                f"input_context field '{offending_field}' looks like a credential. "
                "Remove it and retry; this agent never accepts credentials as caller data."
            ),
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        result = agent.invoke(req.input, ctx=ctx, input_context=req.input_context)
        return cast("dict[str, Any]", result)


@app.get("/health")
def health() -> "dict[str, str]":
    return {"status": "ok", "agent": "SlackChannelMessageAgent"}
