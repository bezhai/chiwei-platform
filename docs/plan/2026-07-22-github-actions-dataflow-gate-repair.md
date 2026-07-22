# GitHub Actions dataflow gate repair

## Problem

`dataflow grep gate` has 211 historical runs with 200 failures. After its last
successful run on 2026-06-01, all 122 subsequent runs failed, including every
push to `main`.

The persistent failure began when the self-built model provider adapters added
transport-level HTTP imports under a directory that the older gate classified
wholly as business code. A later world/life rewrite then exposed a second
problem: the gate scanned the entire application for `insert_idempotent` text,
including comments and data/domain/fetch persistence code outside the current
Business layer. Because the closed-gap job stops at the first failed step,
additional emit, database-session, Redis, and HTTP violations accumulated while
remaining hidden in GitHub's run UI.

The other active workflows do not have the same persistent defect:

- `framework governance gate` failures are expected PR-body policy rejections.
- `Notify Feishu on PR Merge` has no failed run.
- The failed cronjob image workflow was later healthy and has already been
  removed.

## Goal

- Restore `dataflow grep gate` to green on the current repository without
  weakening closed-gap guarantees into count baselines.
- Make the gate enforce the current framework, transport, capability, data,
  and business boundaries.
- Report every closed-gap failure in one run so a leading failure cannot hide
  later regressions.
- Keep the repair locally reproducible and ensure the workflow's own PR can
  satisfy framework governance.

## Non-goals

- No compatibility layer for historical module paths or contracts.
- No product behavior, prompt, schema, queue protocol, or deployment change.
- No redesign of the dataflow runtime or its persistence semantics.
- No change to the Feishu notification workflow or to the framework-spec PR
  policy.
- No merge, production deployment, or recovery of old failed runs.

## Key design decisions

1. **Semantic checks replace misleading text counts where syntax matters.**
   Python call-site checks must ignore comments and docstrings and operate only
   on the current Business layer. Manual emit governance is checked against a
   reviewed source roster rather than a repository-wide total that can be
   preserved by unrelated additions and removals.

2. **Closed gaps remain closed.** The repair removes current database-session,
   raw-Redis, and business-HTTP violations instead of accepting them as a new
   baseline. Existing typed query and capability boundaries remain the required
   route for business code.

3. **Provider adapters are transport code.** The OpenAI and Gemini adapter
   directory is classified as Transport/Capability code and excluded narrowly
   from the business HTTP rule. The exclusion does not extend to other agent or
   life modules.

4. **Single-output nodes use the node contract.** A node with one output returns
   that Data value and relies on the framework wrapper to emit it. Manual emits
   remain only in the reviewed fan-out, streaming, non-node, and deliberate
   side-effect locations.

5. **Raw I/O moves behind existing boundaries.** Product-facing modules keep
   their current return values, error handling, fail-open behavior, timeouts,
   and cache semantics while SQL, Redis, and HTTP transport details move to the
   data-query or capability layer.

6. **Diagnostics are exhaustive.** Closed-gap checks continue after an
   individual failure and a final aggregation step determines the job result.
   This changes reporting, not pass/fail policy.

## Caller coverage

- Model-provider construction continues to use the existing OpenAI and Gemini
  adapters; only their governance classification changes.
- Weather, animation-calendar, and holiday tools retain their existing tool
  signatures and structured success/failure payloads while delegating transport.
- Persona-review notification retains its environment-variable contract,
  payload, timeout, logging, and fail-open behavior while delegating transport.
- Day-page, relationship-page, persona-chain, and owner-identity readers retain
  their existing public functions and result semantics while delegating SQL.
- Life wake-up cooldown keeps the same Redis keys, values, and TTL behavior via
  the typed Redis capability.
- Day-review and persona-review cron wiring keeps the same Data types and
  downstream nodes while using automatic node output emission.
- Every current manual business emit remains covered by an explicit reviewed
  roster.

## Data and deployment impact

- No schema, stored data, queue payload, dynamic configuration, or secret
  changes.
- No new external dependency.
- Application changes are boundary-preserving refactors, but they still require
  the agent-service test suite and an isolated-lane verification before any
  production ship.
- The workflow change itself requires a PR body reference to this spec:
  `Framework-Layer-Spec: docs/plan/2026-07-22-github-actions-dataflow-gate-repair.md`.

## Tasks

1. **Repair semantic gate scope and diagnostics**
   - Goal: enforce current layer boundaries and expose all closed-gap failures.
   - Output: updated gate checks, reviewed manual-emit roster, and exhaustive
     failure aggregation.
   - Acceptance: all gate checks pass locally on the repaired tree; injected
     business-layer violations are detected; one failed check does not skip the
     remaining checks.

2. **Restore business-to-data boundaries**
   - Goal: remove direct session access from business modules.
   - Output: typed data-query entry points used by page, persona-chain, and
     owner-identity readers.
   - Acceptance: public behavior tests remain green and the Gap 13 checks report
     zero business violations.

3. **Restore business-to-capability boundaries**
   - Goal: remove raw Redis and HTTP transport access from business modules while
     preserving their observable contracts.
   - Output: existing Redis capability adoption, typed external-facts and
     persona-notification capability surfaces, and a narrow provider-adapter
     transport classification.
   - Acceptance: focused behavior tests remain green and Gap 14/16 report zero
     unclassified business violations.

4. **Restore the single-output node contract**
   - Goal: remove manual emission from single-output cron adapter nodes.
   - Output: automatic node-return emission and an updated legitimate manual
     emit roster.
   - Acceptance: cron wiring/node tests remain green and the semantic Gap 8
     roster matches exactly.

5. **Validate the complete repair**
   - Goal: demonstrate that the workflow is both green and still meaningful.
   - Output: workflow syntax validation, exact local execution evidence, focused
     application tests, and an independent code review.
   - Acceptance: no current closed-gap violation remains, open-gap counts do not
     increase, and no unrelated worktree change is included.
