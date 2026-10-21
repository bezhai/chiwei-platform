# Agent-Service Framework Layer Governance

This document says which parts of agent-service are the framework, what business code may use of
them, and what a change to the framework needs. How the parts fit together, and why they are
layered this way, is in `docs/guides/plugin-host.md`; this document is the change-control side of
it.

## Layers

### [A] Framework Layer

The framework decides what a plugin, a round, a wake, a manifest, storage, startup and shutdown
mean. Everything a plugin builds against is defined here, so a change here can change the
behaviour of every plugin at once. A pull request that touches these paths must cite a spec
(see Change Rules); `.github/workflows/framework-governance.yml` enforces it and
`.github/CODEOWNERS` asks for the owner's review.

- the plugin host: `apps/agent-service/app/host/**` (plugin lifecycle, services, queues of turns
  and clocks, routes, inboxes and outbound registrations, assembling a round from the manifest);
- the agent layer's contract: `apps/agent-service/app/agent/contracts.py` (the types a round is
  built from: agent and profile definitions, segments, tool declarations, the failure policy of a
  step) and `apps/agent-service/app/agent/round.py` (the fixed loop that runs a participant's
  round);
- the apps: `apps/agent-service/app/manifests/**` (what each app is made of) and
  `apps/agent-service/app/deployment.py` (which manifest each app name runs);
- the operational plugins every app may load: `apps/agent-service/app/plugins/**` (admin
  routes, messaging's operator entry, the skills registry);
- storage: `apps/agent-service/app/data/{schema,migrator,persist,ordered,tx,session}.py` (Data
  classes and table names, the migrator, the write and read primitives, record numbering,
  transactions, the engine); `app/data/queries/` is not part of this layer;
- outbound queues: `apps/agent-service/app/infra/outbound.py`;
- the entrypoint: `apps/agent-service/app/main.py`;
- the rules CI enforces and their scripts: `apps/agent-service/scripts/**` and
  `.github/workflows/**`;
- the framework documents: `docs/guides/plugin-host.md` and `docs/governance/`.

The rest of `app/agent/` (single model calls and their adapters, prompt rendering, the history's
storage and trimming) and `app/messaging/` are framework code as well: business code may not
import their internals (see below). They are left out of the spec requirement because they change
often for reasons that do not alter what a plugin builds against; a change that does alter it is
a change to `contracts.py` or `round.py` and needs the spec anyway.

### [B] Capability Layer

`apps/agent-service/app/capabilities/**` gives typed access to external systems (HTTP services,
Redis, search, weather, the sandbox, image generation). Capabilities raise the exceptions in
`app.capabilities.errors` rather than returning `None` or `False` for a failure, so a caller can
tell "nothing there" from "it went wrong"; a value that means something to the business (an
empty search result when no search backend is configured) is documented on the function.
Business code calls capabilities and does not reach past them to raw Redis, HTTP, RabbitMQ or
database sessions.

### [C] Business Layer

Her features (`apps/agent-service/app/living/<plugin>/`), world's features
(`apps/agent-service/app/world/<plugin>/`, the world core included), and the modules they call
(`apps/agent-service/app/nodes/**`, `apps/agent-service/app/skills/**`). For the import rules
below, `app/plugins/` and the two manifests count as business code too.

Business code registers what it needs through the `ctx` a plugin's setup is given. When it needs
something the host does not offer, the host is extended first (a spec, a new registration on
`ctx`), rather than the business code working around it.

## What business code may use

CI enforces these on every pull request (`.github/workflows/grep-gate.yml`; each script has its
own tests under `apps/agent-service/tests/unit/scripts/`):

- **Plugins talk only through services.** A plugin imports its own directory and its side's shared
  modules, never another plugin (`scripts/check_plugin_boundaries.py`); world and her side never
  import each other, and neither does either app's manifest (`scripts/check_world_life_imports.py`).
- **Only what the framework exports.** Business code imports the host only through the names
  `app.host` exports, and does not import `app.agent.continuity`, `app.messaging.receiving` or
  `app.data.session`; no code imports another package's underscore names
  (`scripts/check_framework_private.py`).
- **Lower layers never import higher ones.** No cycle between packages; storage and messaging do
  not import the agent layer or the host, the agent layer does not import the host, and neither the
  host nor the agent layer imports a plugin (`scripts/check_package_cycles.py`).
- **No background work and no sessions of its own.** Business code starts no task or thread (the
  host runs clocks and tasks and stops them) and does not reach the transaction's session
  (`scripts/check_business_tasks_and_sessions.py`).
- **The manifests resolve.** Both apps' hosts complete setup and the manifest check with no
  external service reachable (the `manifests-resolve` job).
- **The older gaps** (no business retry loops, no direct Redis or httpx, no manual nack, a fixed
  number of `publish` call sites) keep running over the business directories.

## Change Rules

- Any pull request touching [A] must cite a markdown spec in its body with
  `Framework-Layer-Spec: docs/.../*.md`.
- A rule above that blocks a change is changed in its script, with its tests, in the same pull
  request and with the same spec reference; an allowlist entry that exempts one file is not how a
  rule is relaxed.

## Time Source Policy

Clocks (`ctx.clock`) and the clocks behind wakes (`ctx.wake`) are production side effects: they
put participants' rounds on their queues and reconcile what was sent. In deployment lanes:

- `prod` / `blue`: clocks run by default.
- `coe-*` / `ppe-*` / unknown: clocks are skipped by default.
- To test clocks in a lane on purpose, set `DATAFLOW_ENABLE_TIME_SOURCES=1` (the coe ConfigBundle
  already does).

## Outbound Publish Roster

Business code hands something to a channel service only through `app.infra.outbound.publish`, to
a queue a plugin registered with `ctx.outbound`. grep-gate's Gap 8 pins the number of call sites
at 2, both in `app/living/speaking/`:

- `mouth.py`: what she says, part by part.
- `takeback.py`: a recall of something she said.

Each hands something to the outside and learns the outcome only afterwards, which is why neither
runs inside the round's transaction; the landing reconciliation settles what happened. A new call
site needs a reason in the pull request and the pinned count raised with it.
