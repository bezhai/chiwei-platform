# Agent-Service Framework Layer Governance

This document defines the boundary that keeps dataflow/runtime work from
leaking into business code.

## Layers

### [A] Framework Layer

Owns runtime semantics and must be changed only with a spec reference.

- the plugin host: `apps/agent-service/app/host/**`
- the plugins each app is made of: `apps/agent-service/app/plugins/**`
- `apps/agent-service/app/runtime/**`
- the app manifests: `apps/agent-service/app/deployment.py`
- runtime entrypoint: `apps/agent-service/app/main.py`
- framework contracts, governance docs, and CI gates under `docs/guides/`,
  `docs/governance/`, and `.github/workflows/`

Framework changes define what plugins, clocks, admin routes, inboxes, nodes,
wires, durable routing, startup and shutdown, retries and error routing mean.
They must not be hidden inside a business node as a local workaround.

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
- `apps/agent-service/app/living/**`
- `apps/agent-service/app/memory/**`
- `apps/agent-service/app/skills/**`
- `apps/agent-service/app/world/**`

Business code declares Data, nodes, and calls capabilities; a plugin in [A]
registers it with the host. If it needs a new runtime behavior, extend [A]
first instead of bypassing the framework.

## Change Rules

- Any PR touching [A] must cite a markdown spec in the PR body with:
  `Framework-Layer-Spec: docs/.../*.md`.
- Single-output `@node` functions return `Data` and let the wrapper auto-emit.
- Manual `await emit(...)` in business code is allowed only for non-node code,
  genuinely multiple dynamic outputs, streaming segments, or deliberate
  fire-and-forget side effects with local error handling.

## Time Source Policy

Clocks (`ctx.clock`) are production side effects. In deployment lanes:

- `prod` / `blue`: clocks run by default.
- `coe-*` / `ppe-*` / unknown: clocks are skipped by default.
- To intentionally test clocks in a lane, set
  `DATAFLOW_ENABLE_TIME_SOURCES=1`.

## Current Manual Emit Roster

The reviewed business baseline is 3 real `await emit(...)` call sites, all in
`living/` (grep-gate Gap 8 pins the count). Each hands something to the
outside and only learns the outcome afterwards:

- `living/mouth.py`: what she says, segment by segment.
- `living/takeback.py`: a recall of something she said.
- `living/reading.py`: the file she picked up, onto the durable reading edge.

New business `await emit(...)` sites should be treated as framework debt until
the PR explains why one of the allowed cases applies.
