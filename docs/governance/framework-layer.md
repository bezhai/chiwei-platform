# Agent-Service Framework Layer Governance

This document defines the boundary that keeps dataflow/runtime work from
leaking into business code.

## Layers

### [A] Framework Layer

Owns runtime semantics and must be changed only with a spec reference.

- `apps/agent-service/app/runtime/**`
- `apps/agent-service/app/wiring/**`
- `apps/agent-service/app/deployment.py`
- runtime entrypoint: `apps/agent-service/app/main.py`
- framework contracts, governance docs, and CI gates under `docs/guides/`,
  `docs/governance/`, and `.github/workflows/`

Framework changes define what nodes, wires, sources, durable routing,
startup, retries, error routing, and cross-process emit mean. They must not
be hidden inside a business node as a local workaround.

### [B] Capability Layer

Owns typed access to external systems and shared primitives.

- `apps/agent-service/app/capabilities/**`
- stable public facades around infra/runtime internals

Capabilities expose typed errors and domain-shaped methods. Business code can
call capabilities, but should not reach through them to raw Redis, HTTP,
RabbitMQ, DB sessions, or runtime-private modules.

### [C] Business Layer

Owns product behavior.

- `apps/agent-service/app/nodes/**`
- `apps/agent-service/app/agent/**`
- `apps/agent-service/app/chat/**`
- `apps/agent-service/app/life/**`
- `apps/agent-service/app/memory/**`
- `apps/agent-service/app/skills/**`

Business code declares Data, nodes, and calls capabilities. If it needs a
new runtime behavior, extend [A] first instead of bypassing the framework.

## Change Rules

- Any PR touching [A] must cite a markdown spec in the PR body with:
  `Framework-Layer-Spec: docs/.../*.md`.
- Single-output `@node` functions return `Data` and let the wrapper auto-emit.
- Manual `await emit(...)` in business code is allowed only for non-node code,
  genuinely multiple dynamic outputs, streaming segments, or deliberate
  fire-and-forget side effects with local error handling.

## Time Source Policy

Interval sources are production side effects. In deployment lanes:

- `prod` / `blue`: interval sources run by default.
- `coe-*` / `ppe-*` / unknown: interval sources are skipped by default.
- To intentionally test time sources in a lane, set
  `DATAFLOW_ENABLE_TIME_SOURCES=1`.

## Current Manual Emit Roster

The reviewed business baseline is 10 real `await emit(...)` call sites:

- `chat/context.py`: non-node image-content sync side effect.
- `chat/post_actions.py`: post safety and memory trigger fire-and-forget
  emits with local exception handling.
- `nodes/chat_node.py`: router-driven persona fan-out and streaming response
  segment emission.
- `nodes/life_dataflow.py`: glimpse emits per target chat with per-chat
  request ids and per-chat error isolation.

New business `await emit(...)` sites should be treated as framework debt until
the PR explains why one of the allowed cases applies.
