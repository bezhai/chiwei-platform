# Behaviour replay

Replays one kind of round with fixed model replies and compares everything the round did with a
recorded baseline. It is the T0 baseline of the plugin-host refactor
(`~/.claude/specs/agent-service-plugin-host.md`, decision 6): every refactor step must replay
identically, and every intended difference is re-recorded and listed one by one.

Run it as part of the normal suite, or alone:

```bash
uv run pytest tests/replay
```

It needs docker (a real Postgres via testcontainers). Without docker the replays fail instead of
skipping, because a baseline that silently skips guards nothing.

## Layout

| Path | What it is |
|---|---|
| `harness/` | The core. Scenarios should not need to change it. |
| `prompts/<prompt id>.txt` / `.json` | Fixture text for each Langfuse prompt a round renders (text prompt, or a chat prompt as a list of `{role, content}`). |
| `seeds.py` | Rows the rounds read that no round writes (personas, her bot, a private chat). |
| `test_<kind>.py` | Scenarios of one round kind. |
| `test_harness.py` | Self-tests of the core, for guarantees whose breaking no scenario would notice (a swallowed `ScriptExhausted`, Redis shared between scenarios). |
| `baselines/<kind>/<scenario>.json` | The recorded baselines. |
| `.actual/` | Written when a comparison fails; git-ignored. |

## Prompt fixtures

Each fixture is the text of one Langfuse prompt version, copied byte for byte, read-only, with the
langfuse skill (`langfuse_api.py get-prompt '{"name": "<id>", "label": "<label>"}'`); a chat
prompt keeps the `role` and `content` of each message. The label is the one the `coe-world` lane
is served (`app.agent.prompts.get_prompt`): the lane's own label where the prompt has one,
otherwise `production`. The loader reports every fixture as version 1
(`harness/prompts.py::FIXTURE_VERSION`), so the `version` in a baseline is not the Langfuse
version; this table is. Copied on 2026-10-08.

| Fixture | Langfuse prompt | Label | Version | Type |
|---|---|---|---|---|
| `living_life_moment.txt` | `living_life_moment` | `production` | 8 | text |
| `living_day_page.txt` | `living_day_page` | `production` | 2 | text |
| `living_persona_review.txt` | `living_persona_review` | `production` | 2 | text |
| `book_reading_impression.txt` | `book_reading_impression` | `production` | 1 | text |
| `guard_output_safety.json` | `guard_output_safety` | `production` | 5 | chat (system, user) |
| `world_round.txt` | `world_round` | `coe-world` | 6 | text |
| `world_perception.txt` | `world_perception` | `coe-world` | 5 | text |
| `world_answer.txt` | `world_answer` | `coe-world` | 2 | text |
| `world_npc.txt` | `world_npc` | `coe-world` | 2 | text |

Moving a fixture to a newer version is an intended change like any other: replace the text,
update its row, and re-record the baselines that render it.

## What is intercepted, and where

Everything between these boundaries runs for real: the agent loop, tool dispatch, retries,
prompt compilation, transcript storage, the messaging layer's records, claims and retries, the
plugins' setup and the dataflow graph.

| Boundary | Where | What the replay does |
|---|---|---|
| Model calls | `app.agent.client.build_model_client`: `resolve_model_info` resolves every model id to client type `replay`, registered as `ScriptedModel` (`harness/model.py`) | Answers from a script, one queue per agent (the Langfuse prompt the call was rendered from). Records the complete request. Reports usage through `generation_span`, as the real adapters do, so cost rows come from the code under test. The provider adapters never run, so neither does the Gemini adapter's own download of every image url in a request (`_fetch_remote_image`, history included); instead each image block pointing into the object store is recorded with what fetching it at the time of the call returns (`"fetched"`, below). |
| Prompts | `app.agent.prompts._get_client` (the Langfuse SDK client; the lane-label fallback logic stays) | Serves `prompts/<id>`; each prompt object remembers the variables it was compiled with. |
| Tracing | `app.agent.trace._get_client`, `app.agent.core._get_trace_client` | Off (the spans take their no-op path). Not part of the baseline. |
| Clock | `time-machine` (not freezegun: freezegun swaps `datetime.date` for a subclass, which changes the migrator's and persist's type checks and which asyncpg will not encode) | Frozen; moves only when a step says `at=`. The event loop's monotonic clock is untouched. `TZ` is set to the container's `Asia/Shanghai`. |
| File clock | `os.replace` / `os.unlink` / `os.rmdir` under the world volume (`harness/volume.py`) | A written file's mtime is set to the frozen clock (world shows when a record changed). Writes and deletes go onto the effects timeline. |
| Random ids | `uuid.uuid4` (`harness/ids.py`) | Derived from the calling function and how often it has asked, so they are reproducible and one new call does not shift other functions' ids. |
| Dynamic Config | `dynamic_config._get_snapshot` | Reads `replay.config` (key → raw string); unset keys fall back to the code's defaults. |
| Redis | `app.infra.redis._redis` | `fakeredis` (`replay.redis`), on a server of its own per scenario (fakeredis otherwise looks its server up by a `uuid4` host name, which the replay makes deterministic, so every scenario would share one). Only the banned-word set is read today, under the bare key `banned_words`: the Redis capability adds no lane prefix since the 2026-05-13 hotfix (`app/capabilities/redis.py`); a coe lane has a Redis of its own. |
| RabbitMQ | the methods of `app.infra.rabbitmq.mq` (`harness/broker.py`) | In-memory queues, bindings and consumers. Nothing is delivered unless the scenario delivers it (`replay.broker.deliver(queue)`), except replies to a process's own reply queue. Other processes' inboxes are declared with `broker.declare_inbox(name, answers=...)`. A rejected message moves to a dead-letter queue only for isolated routes (their lane's `isolated_dead_letters`); the broker-side dead-lettering of other queues (DLX, lane TTL fallback) is not modelled, so for a durable queue the record is the `reject` itself. A delivery whose consumer was killed is never settled; `replay.broker.requeue_unsettled()` puts it back at the front of its queue, marked redelivered, as RabbitMQ does when the dead consumer's channel closes. |
| Database | real Postgres (`tests/runtime/conftest.py::test_db`); SQLAlchemy engine events (`harness/database.py`) | The full schema both apps run on. Write statements are recorded by transaction; every table is read before and after each step. |
| Object storage and tool-service's image pipeline | `httpx.AsyncHTTPTransport.handle_async_request`, for two hosts only (`harness/objects.py`); requests to any other host go out as before | `tool-service` `POST /api/image-pipeline/get-url` signs a name into `https://object-store.replay/<file_name>?signed-until=<frozen clock + 1.5 h>` (signing is pure computation, as in tool-service: it signs names nothing was stored under). A `GET` on the store answers what the scenario put there (`replay.objects.put(file_name, bytes, content_type)`), `404` for anything else, `403` once the signature has run out. `image_client` (envelope, lane header, error handling), the reading round's byte fetch and the phone's reachability check all run for real above it. Any other tool-service path raises `UnservedRequest` (a `BaseException`, so `image_client`'s `except Exception` cannot hide it): add it to `harness/objects.py`. Every request to these hosts goes onto the effects timeline. |
| Process | `harness/process.py` | Starts an app the way `app.main`'s lifespan does, through its plugin host (`Host.for_app`) with only the broker phases: every plugin's setup, the graph, durable consumers, messaging. No schema step (built once per scenario), interval clocks, HTTP routes or skill reload task; `SKILLS_DIR` is an empty directory. Rounds run when a scenario calls them. The durable consumer's worker name (`app.runtime.durable.WORKER_ID`, `hostname:pid` in production, written on the inflight rows it claims) is `<app>#<n>`, the n-th process the scenario started. |

## What a baseline holds

One JSON document per scenario: the steps in order, and the schema of every tool offered
(`tool_schemas`). Each step records:

- `outcome`: what the step's action returned, or the exception (type and first line).
- `model_calls`: agent, call number, model id, prompt name/version and the variables passed,
  options (`session_id`, `reasoning_effort`, ...), the tools offered (by name), the messages
  sent, and the reply (or the error). Messages are written in full on an agent's first call in
  the step; a later call that continues an earlier one says `"continues": "<agent> #<n>"` and
  lists only the messages added (`"then"`). Structured calls also record the JSON schema.
  An image block (`{"type": "image_url", "image_url": {"url": ...}}`, or `"image"` with `url`)
  whose url points into the object store carries `"fetched"`: what fetching that url at the time
  of the call returns, as `image/png, 69 bytes, sha256:7eaea0ddaf8d`, `404: no such object` or
  `403: signed until 15:30:00, fetched at 15:40:00`. That is what the provider adapter would
  download and send; the url stays as the code built it.
- `effects`: one timeline, in the order things happened: write transactions ending
  (`{"db": "commit", "writes": ["INSERT data_life_moment", ...]}`; one entry is one
  transaction), publishes (target queues, routing key, delay, headers, body, confirmed),
  deliveries and how the consumer settled them, file writes and deletes, and requests to
  tool-service and the object store (`{"http": "POST tool-service/api/image-pipeline/get-url",
  "lane": ..., "request": {...}, "status": 200}`, `{"http": "GET object-store/<file_name>",
  "status": 200, "served": "<content type>, <n> bytes, sha256:<12 hex>"}`). This is where the
  phases of a round's end show (spec decision 3).
- `consumers_started`: queues that got a consumer (the start step lists what the app opens).
- `rows`: per table, rows added / changed / removed by the step.
- `files`: per file on the world volume, added / before→after / removed.

### What is normalised

Nothing is dropped; these values are replaced:

| Value | Shown as | Why it is safe |
|---|---|---|
| ids from `uuid4` during the scenario | `<uuid:N>` by first appearance (a prefix: `<uuid:N>[:k]`) | They are random by design; ids *derived* from a round's identity (`uuid5`) are kept verbatim. |
| columns defaulting to the server clock (`created_at`, `recorded_at`, ...) | `<db-clock>` | Postgres's clock is not frozen. Ordering by them is unaffected. |
| serial / identity columns | `<serial>` | Depend on everything else that ever inserted. |
| `dedup_hash` | `<dedup>` | A hash of the row's Key columns, which are compared verbatim. |
| exception messages | first line only | Later lines are driver boilerplate (SQL, version-specific help URLs). |
| the payload of a base64 `data:` URI | `data:<mime>;base64,<N bytes, sha256:12 hex>` | Bytes are unreadable in a diff; the length and hash still change when the content does. |
| the release in a SQLAlchemy help link (`https://sqlalche.me/e/20/...`) | `https://sqlalche.me/e/<release>/...` | Code that stores `str(exc)` (an inflight row's `last_error`) would otherwise tie a baseline to the library release. |
| bytes fetched over HTTP | `<content type>, <n> bytes, sha256:<12 hex>` | Same as `data:` URIs; the fixture bytes live in the scenario. |

Readability only: timestamps are shown in CST, JSON stored as text is shown parsed, and any
string with line breaks becomes a list of its lines, so a diff points at the line that changed.

## Scenario API (`replay` fixture)

```python
await seeds.seed_household()                        # rows the round reads
replay.broker.declare_inbox("world", answers=...)  # another process's inbox
await replay.start("agent-service")                 # recorded as a step
replay.model.script("living_life_moment", Reply(tools=(ToolUse("switch_to", {...}),)), ...)
await replay.step("first moment", lambda: run_moment(...), at=datetime(...))
replay.check("living_moment/continuation")
```

- `Reply(text=..., tools=(ToolUse(name, args),...), thought=..., thought_signature=b"...",
  usage={...}, data={...})`. `data` answers a structured call. Tool-call ids default to
  `<agent>:<call>:<n>`.
- A script entry can be a function of the `Request` (its messages, tools,
  `request.tool_results()`), for replies that copy something out of the conversation the way the
  model would (see `_take_back_what_she_sent`).
- `Fail(lambda: SomeError(...))`: the provider call raises.
- Every model call needs a scripted entry. A call that finds its agent's script empty raises
  `ScriptExhausted`, which the code under test may swallow (an `except Exception` around a tool,
  the output check's fail-open, `gather(return_exceptions=True)` in a tick), so the step can
  still pass. `replay.check()` therefore fails on any such call, naming the agent and call
  number, before it compares or records anything. Replies left unused fail it too.
- `replay.step(name, action, at=..., raises=ExpectedError)`: run one round (or delivery).
  Anything between steps is not recorded.
- Messages: `replay.message_arrives(sender=, recipient=, body=, message_id=, time=)` puts a
  message on an inbox; `replay.broker.deliver(queue)` hands the oldest one to the consumer;
  `replay.inbox(name)`, `replay.scheduled()` give queue names; `replay.broker.queued(queue)` shows
  what is waiting; `replay.broker.inject(queue, body, headers)` puts anything else on a queue
  (a question: headers `x-reply-rk` and `x-answer-by`).
- Faults: `Fail(...)` (model call), `replay.fail_commits(lambda writes: ...)` (the matching
  transaction fails at COMMIT and rolls back), `replay.broker.refuse_confirms(lambda rk: ...)`
  (a send is not confirmed), `replay.kill_after(lambda effect: ...)` (the process dies right
  after that effect: the next boundary it touches raises `ProcessKilled`; run the step with
  `raises=ProcessKilled`, then `await replay.restart()`). Each fault fires once by default
  (`times=`).
- `replay.config[key] = "value"` for Dynamic Config; `replay.redis` for Redis.
- `replay.objects.put(file_name, data, content_type)`: an object in the store (an attachment the
  inbound pipeline cached, a picture she made). A name nothing was put under signs fine and
  fetches as `404`, which is what a cache miss looks like in production.
- `replay.broker.requeue_unsettled()` after `replay.restart()`: the message a killed consumer was
  handling comes back (see `test_reading.py`'s kill scenarios).

## Adding a round kind

1. Find how production triggers it and call that from a step: a clock-driven function
   (`run_moment`, the nudge tick, `day_page_tick`, `persona_review_tick`) or a delivery to a
   consumer (an inbox, a question queue, the scheduled queue, a durable queue such as
   `durable_file_picked_up_read_a_round_<lane>`). Prefer the outermost entry that production
   calls, so a refactor of what is underneath stays covered.
2. Add a fixture under `prompts/` for every prompt id the round renders, copied from Langfuse
   (not a stand-in: the refactor reshapes real prompts), and add its row to the table above. A
   missing fixture fails loudly with the file name to add.
3. Seed what the round reads in `seeds.py` (shared with other kinds) or in the test.
4. Script the model per agent and write the steps. Cover at least one continuation if the kind
   keeps history, and its fault cases.
5. Record: `REPLAY_RECORD=<kind>/<scenario> uv run pytest tests/replay/test_<kind>.py`. Read the
   baseline before committing it: it is the claim of what the code does today.

If a kind reads something no boundary covers (an HTTP service other than tool-service and the
object store, an image-generation or search provider), add the boundary to the core, document it
in the table above, and make sure the existing baselines still pass. Requests to hosts the
harness does not serve go out to the network unchanged, so a missing boundary shows up as a
connection error in what the step recorded, or as a hang; read the baseline for it.

## Re-recording after an intended change

1. Run the replays; a difference fails with the JSON paths that differ, a unified diff, and the
   full replay written to `.actual/<name>.json`.
2. When every difference is intended, re-record only the affected baselines:
   `REPLAY_RECORD=world_round/continuation uv run pytest tests/replay` (names or globs, comma
   separated; `REPLAY_RECORD=1` records everything the run reaches).
3. `git diff tests/replay/baselines` is the list of intended changes; describe each in the
   commit.

A refactor that moves a boundary itself (for example where model clients are built) changes the
harness adapter in `harness/`, not the baselines.
