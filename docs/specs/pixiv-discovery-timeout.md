# Pixiv discovery timeout

## Goal

Prevent a stalled external call from blocking daily author discovery indefinitely.
The observed worker remained alive after logging one author's start; subsequent
operations had no stage logging, so the exact stalled dependency is unconfirmed.

## Scope and decisions

- Give this worker's Redis commands a finite 10-second timeout, without changing
  defaults for other Redis consumers.
- Bound each discovery external operation to 180 seconds by default; include author
  ID and operation name in start, completion, and failure logs. Configuration uses
  worker environment variables, like existing transport/consumer timeouts.
- Await only one operation at a time. A late response must not resume the caller's
  remaining discovery steps. A timed-out already-dispatched remote write can still
  complete; no rollback or exactly-once guarantee is claimed.
- Continue with the next author after an author failure. Failure notifications are
  bounded and best-effort so they cannot block progress either.
- Update the author's last successful scan time only after successful discovery and
  enqueueing. Preserve existing author cooldown, filtering, and task deduplication.
- Do not change schemas, shared Redis defaults, task consumer semantics, or retry
  historical Dead tasks. Do not merge or release production in this task.

## Callers and deployment

Only the daily download scheduler invokes startDownload, and only dailyDownload
calls DownloadIllusts. The worker Redis singleton also serves existing download and
cache callers; those receive the command timeout but retain all other settings.
Deploy the branch image with all background work disabled in a separate ppe lane;
validate Mongo/Redis connectivity there. Validate timeout and late completion using
local fault injection against the actual discovery function. Live discovery can
then use the existing scheduled entry, within the already authorized author scope.

## Tasks and acceptance

1. Add bounded discovery operations and worker Redis command timeout. Never-settling
   reads must fail with author/stage context, with timers cleaned up on success/error.
2. Integrate all discovery external waits. A failed author and a failed notification
   cannot stop the next author; timeout must not record success or cause late enqueue.
3. Verify and deploy. Relevant tests/typecheck pass, branch image starts and performs
   connectivity checks in the lane, with production deployment unchanged.

## T1 review disposition (Codex)

- Accepted: cancel compound enqueue before insert if its deduplication read completes
  after the deadline; test the actual repository function with a delayed read.
- Accepted: success means every selected candidate was enqueued or already exists;
  zero candidates is successful. A dispatched final Redis success write may time out
  with unknown outcome, and this does not imply it was rolled back.
- Accepted: lane startup uses explicit release overrides disabling schedules,
  consumer, Tagger trigger/projection/callback/result Mongo and historical reconcile.
  Connectivity checks are read-only; source Mongo/Redis still target production.
  Live discovery, if enabled, uses the originally authorized complete followed-author
  set tagged “已上传”, consumer stays off, and production consumes the enqueued work.
  Stop the previous stalled discovery lane before starting this one. Do not run the
  scheduled test across the weekly Bangumi boundary.
- Accepted: cover follower pagination, cooldown read/write, artwork metadata, history,
  ban list, dedup/insert, and notifications. Failure notifications are best-effort;
  other notification timeouts fail that author, as an ordinary discovery failure.
- Clarified: the follower budget covers the entire paginated read (default 180s), not
  each page. It is configurable for large lists. Cancellation stops additional pages
  after a late reply; no author processing can start from a late follower result.

## Verification

- Red: existing code failed all four initial integration scenarios (stalled Redis,
  late proxy, stalled failure notification, and failed enqueue).
- Green: `bun test apps/media-sync-worker packages/pixiv-client`: 207 pass, 0 fail,
  443 assertions. Includes delayed dedup through the real repository and a TCP Redis
  peer that accepts connections without replying.
- `bun run --cwd apps/media-sync-worker check`: passed after adding the missing
  development-only OSS type declaration.
- `bun build apps/media-sync-worker/src/index.ts --compile --outfile=/tmp/media-sync-worker-timeout --external @aws-sdk/client-s3`: compiled 1722 modules.
- T3 Codex review: no required changes; independently reran 14 tests and strict
  typecheck. Optional Redis-singleton-entry test deferred: configuration forwarding
  was inspected, and native command behavior is covered by the TCP fault test.
