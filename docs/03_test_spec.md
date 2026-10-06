# Test Specification - CMN-C2-233 Slack Channel Message Agent

## Test Strategy
- Test types: Unit (per node + service + config + inner graph) / Proof-of-Boundary
  (full outer-graph invoke, import isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end backbone coverage lives in PB-6, which drives the real
  compiled outer graph).
- Adapted from the established tool-calling golden (Kaonavi HR Profile) suite,
  Kaonavi -> Slack, updated to the current test canon.
- The Slack call is exercised through the deterministic, network-free v1 stub
  transport (default) and through monkeypatched fake clients; no live Slack call.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> PII mask -> `execute()` -> credential scan) - never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external gate)
  and `TrustLevel.ANONYMOUS.value` for every other node. The single documented
  exception: `CallSlackApiNode.execute(state, config=...)` config-override test
  (a 2nd argument `__call__` cannot forward). The trust-gate rejection test asserts on the
  RETURNED error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in
  `error_log`, execute-only keys absent) - `__call__` never raises for a trust-gate denial.
- Assertion contract (wheel `agenticstar-agentcore==1.0.0`): the invoke surface is
  `result["output"]` / `status` / `trace_id` / `correlation_id` / `node_history`
  (never `formatted_output` at the invoke surface); status is compared to
  `AgentStatus.SUCCESS`/`.value` (lowercase `success`/`error`); the outer graph is
  called as `invoke(user_input=..., ctx=...)`; identifiers may be masked
  (`[MASKED]`) so record evidence is asserted by presence, not raw repr; audit
  spies assert on `call.args[1]` (the event payload), never the whole-call repr.
- Real-SDK pipeline behaviors encoded by the suite: `__call__` short-circuits on an
  incoming errored state (execute() is skipped; error status/error_log pass through);
  the framework mask rewrites Title-Case bigrams (across newlines), emails/'@',
  and digit groups in `user_input`/`validated_input` to `[MASKED]` before `execute()`
  sees the text - positive payloads are PII-free (lowercase channel/member tokens,
  member handles on a `Members:` line instead of `@handle`, ID-shaped hints in the
  non-scanned `channel_hint` state field), intentional-PII tests assert the
  `[MASKED]` path.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary (file renamed from the layer-numbered original): ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in error_log, execute-only keys `validated_input`/`channel_hint` absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | caller-contract validation (type/shape/length bounds per field, refusals naming the field and never the value) + template-owned screens asserted through a DIRECT `execute()` call, both directions (mrkdwn escapes stripped and email/token redacted on BOTH the instruction and the caller `message` channel; ordinary request text and legitimate look-alike wording left intact) |
| U-02b | test_pre_process_node.py | serialize NL request + channel hint into `validated_input` (JSON); mrkdwn control-sequence strip (`<!channel>`); hint priority channel > channel_hint > channel_id | hint resolved by priority; `<!channel>` stripped; empty/missing -> `status=error`; the audit payload carries hint presence only |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`, Slack `xox[abeprs]-*`) | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; the audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = post_message / create_channel / invite_member (keyword, channel-management-first priority); **write-only agent: no-signal -> HARD error, never a guessed write** | correct intent per keyword; "create the channel ... and post ..." -> create_channel; no-signal -> `status=error` ("refusing to guess a write"), no `intent` key; empty -> error; the audit event carries the intent label only |
| U-05 | test_infer_slack_fields_node.py | channel resolution (`#name` in text > "channel called X" > `Channel:` line > shape-conforming hint; never invented); quoted / `Message:` / "saying ..." message body; `Members:` line member handles; Slack Web API `{method, args}` payload per intent (JSON string) | post `chat.postMessage {channel, text}`; create `conversations.create {name, is_private}`; invite `conversations.invite {channel, users}`; ID-shaped hint -> channel arg, `channel_name` stays ""; unresolved channel left `""`; empty input -> error |
| U-06 | test_call_slack_api_node.py | post/create/invite via the network-free v1 stub; `slack_config` state field + `execute(state, config=...)` override (documented direct-execute exception); API error / unresolved channel / empty text / no users / unknown method / missing payload; secret posture (live transport refuses to run unauthenticated; token read via `ctx.secrets`, never env/state) | channel_id/permalink (+ message_ts for post) on success; 403 surfaces in error_log; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to client; the audit event carries presence signals with `stub_transport=True` |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id/ts formatting; `#name` label with channel_id fallback | "Posted message to channel / Created channel / Invited members to channel ... ref=... id=... ts=..."; missing evidence -> error; the audit event carries intent + permalink presence |
| U-08 | test_post_process_node.py | `formatted_output` shaping (payload round-trip); errored state passes through `__call__` un-masked (short-circuit); record-evidence output gate (module-level `_security_gate_output()` helper, full-node path + direct function tests incl. Bearer- and `xox*-`-shaped values); **containment of a blocked response** | success shape with parsed `slack_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without channel_id/permalink blocked ("output gate"); a block clears `result` and every answer/payload field, leaves a TRUTHY withheld notice in `formatted_output`, names the location and not the value, and returns a delta the framework output scan finds nothing in |
| U-09 | test_slack_client.py | Slack Web API client: chat.postMessage / conversations.create / conversations.invite; `Authorization: Bearer` header; `SlackApiError` on non-2xx AND on `ok:false` at HTTP 200 (Slack-specific); stub shapes (deterministic ids, ID-echo, `_stub` marker); `uses_stub_transport` | correct URLs/headers/bodies (name `#`-stripped, users comma-joined); 429/ok:false raise with the Slack `error` string; stub deterministic shapes |
| U-10 | test_config.py | flat `config/agent.yaml` manifest + `config/config.yaml` runtime parameters, AND the propagation of those values to their consumers | id CMN-C2-233, Cat 2, CMN, ToolCallingAgent, namespace `cmn`, dotted `class:` entry point, VERIFIED_EXTERNAL, `generation_mode: deterministic`, `requires.secrets`/`extras` both empty, no nested `agent:` block, no retired `timeout_seconds` key; `max_retry`/`timeout_s`/`slack` reach `_parent_config()`, the inner graph's `slack_config` state field, and the outer graph's own `config` |
| U-12 | test_framework_compliance_tc06_tc07.py | framework-contract compliance: domain nodes may extend the input/output gates only through the `_extra_*` hooks | TC-06: overriding the default input gate raises at class definition; TC-07: overriding the default output gate raises at class definition |
| U-11 | test_domain_workflow_graph.py | inner `SlackWorkflowGraph`: identity, `_extra_initial_state()` slack_config JSON injection + caller-context seeding, `route()` error short-circuit, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as JSON string; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: 0 Level-0 imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external-trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, SlackWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (no graph class declares `propagate_hitl=True`; no cross-boundary interrupt checkpoint): module-level skipif; the stub body is a real AssertionError so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |
| PB (E2E) | Caller contract through the real ASGI `/invoke` | test_invoke_e2e.py | Bearer auth enforced (401 without/with a wrong token); real record evidence derived from the request (channel_id, message_ts, permalink) and output that tracks the input rather than a constant; a caller-only channel reaches the workflow AND the same instruction fails without it (the bridge, proven both ways); post/create/invite paths reachable; unclassifiable request refused; every caller field fails CLOSED on a wrong type, a bad shape, an over-cap size and on bare `NaN`/`Infinity`/`-Infinity` sent over the wire; rejections never echo the value; adapter caps return 413; no credential-shaped value in the response; workspace identifiers returned byte-identical; a response the output gate blocks carries no released text, no record evidence, no traceback and no source paths — only a withheld notice — while an unaffected request still returns its answer; credential-shaped caller context is refused `400` naming the field (masked when the key is undeclared) without echoing the value, and ordinary domain text on the same field still succeeds |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy
> tests (every node asserts on the event payload, `call.args[1]`). PB-3 (live
> external service) is exercised at first-invoke against a live workspace, not in this suite - the v1
> transport is the documented network-free stub.

## Test Execution Summary
- Execution date: 2026-08-31
- Runner: `python -m pytest tests/ -v` against the real `agenticstar-agentcore` 1.0.2 wheel
- Total tests: 228
- Pass: 227 / Fail: 0 / Skip: 1 (PB-7 - not applicable, non-HITL)
