# Agent-Service Framework Layer Governance

This document defines the boundary that keeps dataflow/runtime work from
leaking into business code.

## Layers

### [A] Framework Layer

Owns runtime semantics and must be changed only with a spec reference.

- `apps/agent-service/app/runtime/**`
- `apps/agent-service/app/wiring/**`
- `apps/agent-service/app/deployment.py`
- runtime entrypoints: `apps/agent-service/app/main.py`,
  `apps/agent-service/app/workers/runtime_entry.py`
- framework contracts, governance docs, and CI gates under `docs/guides/`,
  `docs/governance/`, and `.github/workflows/`

Framework changes define what nodes, wires, sources, durable routing,
startup, retries, error routing, and cross-process emit mean. They must not
be hidden inside a business node as a local workaround.

### [B] Data, Capability, and Transport Layer

Owns persistence access, typed access to external systems and shared
primitives, and provider wire adapters.

- `apps/agent-service/app/data/**`
- `apps/agent-service/app/capabilities/**`
- `apps/agent-service/app/agent/adapters/**`
- Data persistence helpers under `apps/agent-service/app/domain/**` and
  `apps/agent-service/app/fetch/**` may call the reviewed runtime persistence
  primitives; they must not expose raw sessions to Business code.
- stable public facades around infra/runtime internals

Capabilities expose typed errors and domain-shaped methods. Business code can
call capabilities, but should not reach through them to raw Redis, HTTP,
RabbitMQ, DB sessions, or runtime-private modules.

Provider adapters translate the neutral model-client contract to a provider's
native SDK/wire protocol. They may own provider transport details such as
`httpx`; the current raw-HTTP roster is exactly `agent/adapters/openai.py` and
`agent/adapters/gemini.py`. Adding another transport site requires an explicit
roster review and does not make raw HTTP a Business-layer API.

### [C] Business Layer

Owns product behavior.

- `apps/agent-service/app/nodes/**`
- `apps/agent-service/app/agent/**` except `agent/adapters/**`
- `apps/agent-service/app/chat/**`
- `apps/agent-service/app/life/**`
- `apps/agent-service/app/memory/**`
- `apps/agent-service/app/skills/**`

Business code declares Data, nodes, and calls capabilities. If it needs a
new runtime behavior, extend [A] first instead of bypassing the framework.

## Change Rules

- Any PR touching [A] must cite a markdown spec in the PR body with:
  `Framework-Layer-Spec: docs/.../*.md`.
- [A] changes must cover both FastAPI lifespan and worker `Runtime.run()` when
  the behavior affects startup, source loops, consumers, or emit semantics.
- Single-output `@node` functions return `Data` and let the wrapper auto-emit.
- Per-key fan-out uses `wire(Data).fan_out_per(extractor)`, not hand-written
  persona loops.
- DB mutation followed by emit uses `emit_tx` / outbox semantics, not
  commit-then-emit in business code.
- Manual `await emit(...)` in business code is allowed only for non-node code,
  genuinely multiple dynamic outputs, streaming segments, or deliberate
  fire-and-forget side effects with local error handling.

## Time Source Policy

Cron and interval sources are production side effects. In deployment lanes:

- `prod` / `blue`: cron and interval sources run by default.
- `coe-*` / `ppe-*` / unknown: cron and interval sources are skipped by
  default.
- To intentionally test time sources in a lane, set
  `DATAFLOW_ENABLE_TIME_SOURCES=1`.

MQ sources are not disabled by this policy; workers still need lane-scoped
queue consumption for normal verification.

## Current Manual Emit Roster

The reviewed business roster is 9 real `await emit(...)` call sites:

- `chat/context.py` (1): non-node image-content sync side effect.
- `chat/post_actions.py` (1): post-safety fire-and-forget emit with local
  exception handling.
- `nodes/chat_node.py` (5): router-driven persona fan-out and streaming
  response segment emission.
- `nodes/life_tools.py` (2): tool-driven proactive-send and reading side
  effects with their own idempotency/error handling.

New business `await emit(...)` sites should be treated as framework debt until
the PR explains why one of the allowed cases applies. The CI gate matches this
roster by source file and enclosing function; it does not accept a replaceable
repository-wide count.
