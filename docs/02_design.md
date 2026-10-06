# Template Design Specification — CMN-C2-233 Slack Channel Message Agent

## Position in AgentCore Architecture

- **Agent Class**: `SlackChannelMessageAgent` (`src/graph/graph.py`)
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Agent Type**: ToolCallingAgent — classify intent -> extract Slack
  channel/message/member fields -> build a Slack Web API request -> call the
  tool -> format the confirmation. No RAG retrieval, no autonomous ReAct loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: framework base-class inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | serialize NL request + channel hint into `validated_input` (JSON); mrkdwn control-sequence sanitize | user_input, input_context | validated_input, channel_hint | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Slack workflow subgraph | validated_input | result, intent, channel_id, channel_name, message_ts, permalink, confirmation, slack_payload | GraphNode (caller ctx forwarded unchanged) | SlackWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; module-level `_security_gate_output()` scan | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------------|
| validate_input | 1 ValidateInput | empty/non-request guard; deterministic (regex) flag-and-redact of email/token-like strings (incl. Slack `xox*-` tokens) before logging | validated_input, channel_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> post_message / create_channel / invite_member; low-confidence/unknown -> **hard status=error** (this is a write-only agent with no read-only default — never guess a write) | intent | ANONYMOUS |
| infer_slack_fields | 3 InferSlackFields | extract channel (`#name` / "channel called X" / "Channel:" line / caller hint), quoted message text, `@user` mentions; assemble the Slack Web API request per intent; an unresolved channel is left empty (never invented) | channel_name, message_text, invite_users, slack_payload | ANONYMOUS |
| call_slack_api | 4 CallSlackApi | chat.postMessage (post) / conversations.create (create) / conversations.invite (invite) via `SlackClient`; token via ctx.secrets; API error -> status=error | channel_id, channel_name, message_ts, permalink | ANONYMOUS |
| confirm | 5 Confirm | format intent + channel id + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / SlackWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_slack_fields
              -> call_slack_api -> confirm -> END
```

Structured parameters travel as a JSON string: `pre_process` serializes
`{"text", "channel_hint"}` into `validated_input`,
`SlackWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
the first inner node (`validate_input`) parses it back. Inner nodes read
`state.get("validated_input") or state.get("user_input", "")`.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (the state contract —
fields are absent until their producer node writes them). Dict/list payloads
are stored as JSON strings (`Optional[str]`) via the module helpers
`to_json` / `from_json`, used by every producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| channel_hint | NotRequired[str] | caller-supplied channel id/name hint; never inferred | pre_process / validate_input |
| channel_id | NotRequired[str] | Slack channel ID returned by the Slack Web API | call_slack_api |
| channel_name | NotRequired[str] | human-readable channel name resolved from the request | infer_slack_fields / call_slack_api |
| redaction_flags | NotRequired[Optional[str]] | JSON list of patterns redacted before logging | validate_input |
| message_text | NotRequired[str] | message body to post (post_message intent) | infer_slack_fields |
| invite_users | NotRequired[Optional[str]] | JSON list of member handles to invite (invite_member intent) | infer_slack_fields |
| slack_payload | NotRequired[Optional[str]] | JSON — assembled Slack Web API request `{method, args}` (msgpack-safe: stored as a JSON string via `to_json`/`from_json`) | infer_slack_fields |
| slack_config | NotRequired[Optional[str]] | JSON — manifest `slack:` section forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| message_ts | NotRequired[str] | Slack message timestamp id returned by chat.postMessage | call_slack_api |
| permalink | NotRequired[str] | human-readable record reference (`slack://channel/<id>[/message/<ts>]`) | call_slack_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the Slack bot token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration Forwarding (nested Cat-2)

Nodes take **no constructor arguments** (SDK v1 nodes are no-arg; configuration
never rides on node instances).

Runtime parameters live in `config/config.yaml`; `config/agent.yaml` is the
static registry manifest and holds no runtime block.
`SlackWorkflowGraphNode._parent_config()` reads `config/config.yaml` and
forwards the `slack:` section plus the runtime values (`max_retry`,
`timeout_s`) to the inner graph under `config["configurable"]` — never `{}`.
The inner graph's `_extra_initial_state()` then injects the `slack` section
into State as a JSON string (`slack_config`), where
`CallSlackApiNode.execute(state, config=None)` reads it (an explicit
`config["configurable"]["slack"]` override is also honoured for direct/unit
invocation).

A reader aimed at the wrong file returns `{}` and every declared value goes
quietly dead while the tests stay green, so `tests/unit/test_config.py`
asserts the values ARRIVE — at `_parent_config()`, at the inner graph's state,
and on the outer graph's own `config` — rather than only that they are
declared.

`src/api/server.py` loads the same file via `load_runtime_config()` and passes
it as `config=`. Without that, the framework's own `max_retry` routing would
run on its built-in default no matter what the file said.

## Caller-Data Contract

Structured caller data arrives on `input_context` beside the free-text
instruction and is validated in `PreProcessNode`, the node that owns the
contract:

| Field | Type | Bound |
|---|---|---|
| `channel` / `channel_hint` / `channel_id` | string | a channel ID (`^[CG][A-Z0-9]{6,20}$`) or a channel name (`^[a-z0-9][a-z0-9._-]{0,79}$`) |
| `message` | string | sanitized, <= 2000 characters |
| `users` | list of strings | <= 50 entries, each an inert handle `^[A-Za-z0-9._-]{1,32}$` |

Rules: values must be the declared TYPE and are never coerced — `str(float("nan"))`
is `"nan"`, which satisfies a channel-name check, so a coercing validator would
aim a write at whatever fell out. A refusal names the field and never echoes the
value. The adapter additionally caps the envelope (16 keys, 256KB). Absent
fields simply degrade to what the instruction text yields.

The adapter also refuses caller context that carries a **credential-shaped
value**, before the agent is invoked at all. This is not a stylistic
preference: the first backbone node copies `input_context` verbatim into its
own node result, and the framework output gate scans every value of every
result — so such a request dies inside that first node with a stack trace and
no reason the caller can act on, before any validation this template owns has
run. The request cannot succeed either way, so the adapter refuses it with
`400` and names the offending field (`credential_bearing_field()` in
`src/api/server.py`). The scan is the framework's own
`detect_credentials_in_value`, the same function the gate calls, so the refused
set and the blocked set are identical by construction rather than by
maintenance. The refusal names the field only; an undeclared key is
caller-controlled text and is reported as `<unrecognised-field>`.

The template accepts **no caller-supplied numbers**, so there is no numeric
threshold to parse; the type guard is what keeps non-finite values out, and the
test suite drives bare `NaN` / `Infinity` / `-Infinity` through the real route
for every field to prove it.

### Crossing the graph boundary

`GraphNode.execute()` invokes the inner graph without forwarding
`input_context`, so an inner read of that field would always see `{}`. The
validated contract crosses on a ContextVar (`src/graph/context_bridge.py`):
`SlackWorkflowGraphNode.extract_input()` stashes it, and the inner graph's
`_extra_initial_state()` seeds it. The ContextVar is per thread/task, so
concurrent invocations cannot see each other's data.

It deliberately does NOT ride inside the `validated_input` JSON: the framework
masks that field at every node boundary, so caller values can be rewritten
between hops. That channel is also not covered by the framework's input scan,
which is why `PreProcessNode` screens its free-text fields itself: caller
`message` text goes through the SAME mrkdwn strip and the SAME
email/token flag-and-redact that the instruction text gets. Both helpers live
in `src/services/security.py` and are shared by the outer `pre_process` and the
inner `validate_input` step, so neither inbound channel can be the unscreened
one. `tests/unit/test_pre_process_node.py` proves it by calling `execute()`
directly, with no framework wrapper in front, and probes the other direction
too: ordinary requests naming channels and people pass through unchanged.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the write-capable `CallSlackApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation).
- **Input sanitize + flag-and-redact** — the backbone `pre_process` strips
  Slack mrkdwn control sequences (`<...>` — mass-mention `<!channel>`/`<!here>`
  and link-injection candidates) and caps length before serialization
  (`src/services/security.py`). `ValidateInputNode.execute()` then runs a
  deterministic (regex, NOT LLM) scan for email addresses and access-token-like
  strings (Slack `xox*-...`, `eyJ...`, `secret_...`, `sk-...`) and redacts them
  before any logging. A Slack request legitimately names channels and members,
  so this is flag-and-redact for safe logging, not a hard reject; the framework
  PII mask in `BaseNode.__call__` additionally masks emails/phones/names in
  `user_input`/`validated_input`. The only deterministic auto-reject is the
  empty/non-request guard.
- **Secrets + output gate** — the integration token is read via
  `ctx.secrets.get("SLACK_BOT_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State; declared under
  `config/agent.yaml requires.secrets`. A missing token is tolerated **only**
  while the deterministic network-free stub transport is active (no live call is
  made); with a live transport injected, a missing token is a hard
  `status=error`. The domain output gate is the **module-level**
  `_security_gate_output()` in `src/nodes/post_process_node.py`, called from
  `PostProcessNode.execute()`: it blocks any SUCCESS response that lacks record
  evidence (channel_id/permalink) and any credential-shaped string in
  `formatted_output`. A block **empties** the response as well as failing it:
  `result`, `formatted_output` and every workflow field carrying answer text or
  payload are cleared, because the caller envelope is composed as
  `formatted_output or result` without consulting status — an error status
  alone would publish the refused answer under an error label.
  `formatted_output` is replaced by a withheld notice rather than blanked,
  since a falsy value hands the envelope straight back to `result`. Violation
  strings name the LOCATION and never the matched value: the cleared delta is
  itself scanned by the framework output gate, and a quoted credential would
  make that gate raise, replacing the whole return with a bare error partial
  and undoing the clearing. No node defines `_extra_security_gate_input/_output`
  instance methods (framework hooks are @final / auto-wrapped — domain checks
  live inline or in module-level helpers).
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (intent / presence signals only — never request text, message
  content, or credentials). `__call__()` is never overridden; `_invoke_impl`
  is never defined on any node. Event names (documented for operations):

  | Node | Audit event |
  |------|-----------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_slack_fields | `infer_slack_fields_complete` |
  | call_slack_api | `call_slack_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` |

  One additional event, `post_process_output_withheld`, is emitted on the
  output gate's block path (violation COUNT only — never a location, never a
  value), so a withheld response is visible in the trace rather than silent.

## Structured product surface (`get_output()` override)

`SlackChannelMessageAgent.get_output()` **extends** `super().get_output()`:
on SUCCESS — and only when the gated `formatted_output` carries record
evidence (channel_id/permalink) — it additionally surfaces the structured keys
`intent`, `channel_id`, `channel_name`, `message_ts`, `permalink`,
`confirmation` at the top level of the invoke result, so callers can consume
the outcome without parsing the confirmation string. On any non-SUCCESS status
(or missing evidence) the structured keys are withheld — fail-closed, matching
the output gate.

### Output invariant

This template renders **no monetary aggregates and no computed figures**, so the
fleet's round-to-the-nearest-1,000 precision grid does not apply here. Applying
one would be actively harmful: a Slack message timestamp (`1785404632.053269`)
is a bare numeric run with no letters to protect it, and a channel ID such as
`C0B1BLJPH2A` is an opaque identifier — a numeric-rewriting rule would corrupt
both. Nothing in this template rewrites numbers on the way out, and
`tests/proof_of_boundary/test_invoke_e2e.py` pins that: workspace identifiers
must cross the output boundary byte-identical.

The invariant this template DOES enforce is its own: a SUCCESS response must
carry record evidence (`channel_id` or `permalink`), and no credential-shaped
string may reach the caller. The gate enforces that over **every
representation** of the output — it walks nested mappings and lists, not just
top-level strings, because `slack_payload` is a nested mapping whose `args`
carry caller-derived text. A scan limited to top-level values steps straight
past a credential in the message body.

## v1 Implementation Note — LLM synthesis

v1 is fully deterministic: intent classification (`ClassifyIntentNode`) uses a
keyword heuristic and field inference (`InferSlackFieldsNode`) uses regex/
line-structure extraction, so the template runs and tests without a live LLM.
**No LLM client is constructed anywhere in v1** and no `system_prompt` is read
(no dead config). LLM-backed synthesis (richer intent classification,
free-text-to-field mapping, natural-language channel summaries) is a documented
follow-up: the manifest `llm:` section — when declared — is already forwarded
to the inner graph by `_parent_config()`, so wiring an LLM in is additive and
requires no graph-shape change.

## v1 Limitation — Slack client (documented)

`src/services/slack_client.py` mirrors the established tool-calling golden's
service shape (injectable transport, `SlackApiError`, per-call token, no
framework imports) but ships a **deterministic, network-free v1 stub** as its
default transport: it returns the documented Slack Web API response shapes
(`{"ok": true, "channel": ..., "ts": ...}` for chat.postMessage; the
`{"ok": true, "channel": {...}}` envelope for conversations.create /
conversations.invite, with synthetic ids derived from the request) so the
pipeline is runnable and testable without a live Slack workspace or the
`requests` package. It does **not** perform a live Slack call. This follows the
Build policy: never fake a live call — document the limitation. To go
live, inject a real `post` transport at construction; the method contracts and
payload shapes are already Slack Web API exact (`chat.postMessage`,
`conversations.create`, `conversations.invite`), so no business-logic change is
required. (The stub also runs without a live credential — see Secrets above; a live
transport requires `SLACK_BOT_TOKEN`. Slack-specific: an `{"ok": false,
"error": ...}` body is raised as `SlackApiError` even on HTTP 200.)

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallSlackApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallSlackApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("SLACK_BOT_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`. The lookup is optional and the shipped transport runs without a credential, so `config/agent.yaml requires.secrets` declares nothing: a declared-but-unprovisioned secret fails the agent at compile time.
- [x] Audit `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events (node_start/node_complete/node_error/s1_denied) NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `SlackWorkflowGraph` (`BaseGraph`) via `SlackWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `SlackWorkflowGraphNode._parent_config()` reads
  `config/config.yaml` and forwards `{slack, agent: {max_retry, timeout_s}}`
  under `config["configurable"]` to the subgraph.
- **Caller-context forwarding**: the validated caller contract crosses on the
  ContextVar in `src/graph/context_bridge.py`, because the framework does not
  forward `input_context` into a subgraph.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no Level-0 SDK import anywhere
- [x] `src/services/slack_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Framework base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat — `gate-composition`; 5 domain steps live in the inner graph |
| LLM dependency | LLM client in v1 | deterministic v1, LLM as documented follow-up | deterministic v1 | template runs/tests without a live LLM; no dead prompt/config reads; see "v1 Implementation Note — LLM synthesis" |
| Slack client | live `requests` call | injectable transport + documented v1 stub default | injectable + v1 stub default | No live network by default; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + manifest forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | SDK v1 nodes are no-arg (ctor args TypeError at graph build); manifest stays the single config source |
| Write target | infer channel from NL freely | explicit channel (`#name` / hint) only; unresolved left empty | explicit only | never post to / create / invite into the wrong channel; unresolved channel -> status=error, not invented |
| Unknown intent | default to post_message | hard status=error | hard error | every supported operation is a WRITE (no read-only default exists); a low-confidence classification must never guess a write — channel-management phrases are matched strictly, and no-signal requests are refused with a clear error |
| v1 operation scope | all 7 proposal actions | post_message / create_channel / invite_member | 3 core writes | deterministic v1 covers the notification-dispatch job end-to-end; DM/file/topic/thread actions are additive follow-ups on the same `{method, args}` payload shape |
