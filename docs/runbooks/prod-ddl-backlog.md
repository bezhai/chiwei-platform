# prod DDL backlog

Schema changes that prod needs but no code path will apply on its own. This file is
the single source for them: when a branch introduces one, add it here instead of
leaving it in a commit message or a conversation, and clear it at ship time.

Two things do not belong here:

- **dataflow `Data` tables.** `Runtime.migrate_schema()` runs unconditionally on
  startup (`apps/agent-service/app/main.py:55`, `app/workers/runtime_entry.py:38`),
  on every lane including prod. New `data_*` tables and new columns on tables that
  are still declared get created automatically. See "What migrator covers" below for
  the one precondition.
- **Fields added inside an existing JSONB column.** No DDL involved.

Everything else — columns on `common_message`, `model_provider`, `bot_config` and the
rest of the common layer, plus any index the ORM does not build — has no automatic
mechanism behind it and must be listed here.

## Pending

### Old world layer: drop `lasts_until`, drop seven tables

The old world round, calendar and upcoming items are deleted from agent-service
(`Happening.lasts_until`, `WorldRound`, `Upcoming` and their writers are gone). Same DDL
for prod and chiwei-test; every statement carries `IF EXISTS` because the two databases
did not see the same code. `lasts_until` was added on this branch (`d211c960`) and never
reached main, so it exists where this branch ran (chiwei-test through coe-living, and the
prod database only if a ppe lane of this branch ever ran).

**Order matters for the column, and it is the opposite of the tables.** The migrator
refuses to start when a still-declared table has a column its class no longer has
(`migrator.py:218-224` → `MigrationError: column data_happening.lasts_until dropped from
Happening`), so the first release without the field crash-loops until this runs. The
old code writes `lasts_until` on every `Happening` insert, so dropping it while the old
code still runs breaks her `say` / `act`. Stop the old agent-service in that lane, run
the column drop, then start the new release. Its index
(`ix_data_happening_lane_lasts_until`) goes with the column. Rows with
`actor = 'world'` stay.

```sql
ALTER TABLE data_happening DROP COLUMN IF EXISTS lasts_until;
```

The tables have no reader or writer after the release (the migrator only touches
declared classes, so their presence does not block startup). Drop them after the
release:

```sql
DROP TABLE IF EXISTS data_world_round;
DROP TABLE IF EXISTS data_upcoming;
DROP TABLE IF EXISTS data_world_arc;
DROP TABLE IF EXISTS data_world_attention;
DROP TABLE IF EXISTS data_world_outline;
DROP TABLE IF EXISTS data_world_state;
DROP TABLE IF EXISTS data_npc_roster;
```

### Location-based perception: drop five columns from `data_happening` and `data_life_moment`

Phase 2 of the life/world split removes the code that decided, by place, which sister
perceived another sister's happening. What reaches her from others now arrives in her
inbox (`data_received_message`, created by migrator). The columns that only served the
old path are gone from the classes:

- `Happening.place` and `Happening.who_was_where` (the place string each happening was
  compared against, and the snapshot of where everyone was when it happened);
- `LifeMoment.after_seq`, `LifeMoment.next_seq` and `LifeMoment.perceived` (the
  per-round cursor over everyone's happenings, and how many she perceived).

Same DDL for prod and chiwei-test, each statement with `IF EXISTS`. None of these
columns carries an index of its own.

**Order matters, same as `lasts_until`.** The migrator refuses to start while a
still-declared table has a column its class no longer has (`migrator.py:216-224` →
`MigrationError: column data_happening.place dropped from Happening`), so the first
release without the fields crash-loops until this runs. The old code writes all five on
every insert (`place` and `who_was_where` on each `say` / `act` / phone send /
take-back, the three cursor columns on each round), so dropping them while the old
release still runs breaks her rounds. An old release that restarts after the drop also
adds the columns back (its migrator sees them missing from the table), after which the
new release refuses to start again.

So the stop covers **every agent-service release on the same database**, not one lane:
the tables are shared by all lanes on that database (prod together with every `ppe-*`
lane; chiwei-test with every `coe-*` lane). Undeploy each old agent-service release on
that database (undeploy, not just a restart, so nothing brings it back), run the drop,
then start the new release. Only agent-service declares these tables; the world App
imports none of the living code and is not affected.

Existing rows keep everything else; rows written by other sisters stay in the table
and are simply never read across residents again.

```sql
ALTER TABLE data_happening DROP COLUMN IF EXISTS place;
ALTER TABLE data_happening DROP COLUMN IF EXISTS who_was_where;
ALTER TABLE data_life_moment DROP COLUMN IF EXISTS after_seq;
ALTER TABLE data_life_moment DROP COLUMN IF EXISTS next_seq;
ALTER TABLE data_life_moment DROP COLUMN IF EXISTS perceived;
```

### `message_record` (messaging record)

Declared at `apps/agent-service/app/data/models.py` (`MessageRecord`); written and read
with raw SQL in `app/messaging/record.py`. coe-* lanes get it from
`ensure_business_schema()`; chiwei-test already has it. Recording is part of every
send, so without the table every send, ask and scheduled delivery fails with
`SendFailed` — apply before the first prod release that runs messaging.

```sql
CREATE TABLE message_record (
    id           BIGSERIAL PRIMARY KEY,
    lane         TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    sender       TEXT NOT NULL,
    recipient    TEXT NOT NULL,
    body         TEXT NOT NULL,
    message_time TIMESTAMPTZ NOT NULL,
    in_reply_to  TEXT,
    outcome      TEXT NOT NULL,
    reason       TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX message_record_message_idx ON message_record (message_id);
CREATE INDEX message_record_lane_time_idx ON message_record (lane, recorded_at DESC);
```

## Applied

Already present on prod. Listed so nobody re-derives them as pending.

Verified by querying `information_schema.columns` and `pg_indexes` on 2026-09-11, and
again on 2026-09-12 for the table count and the `life-model` row.

### `life-model` points at the Gemini provider

```sql
UPDATE model_mappings
   SET provider_name = 'gemini', real_model_name = 'gemini-3.7-flash'
 WHERE alias = 'life-model';
```

Ran as mutation 256 on 2026-09-11, right after agent-service was released — the order
mattered, because until then the alias was live under the old engine, which had never
run on Gemini. Her moment turn resolves it at `app/living/moment.py:165`.

`api_version` stays NULL on the `gemini` provider: that row points at Google's own
endpoint, where the SDK default (v1beta) is the shape that works. Pinning v1 is what
the internal gateway needed.

### Columns

| Change | Declared at | Commit |
|---|---|---|
| `common_message.mentioned_common_user_ids uuid[]` | `app/data/models.py:113`, `packages/ts-shared/src/entities/common-message.ts:56` | `5c3cda5a` |
| `common_message.agent_outbound_id uuid` | `app/data/models.py:127`, `common-message.ts:90` | `23d40c89` |
| `ix_common_message_agent_outbound_id` on the above | `common-message.ts:12` | `23d40c89` |
| `common_message.recalled_at timestamptz` | `app/data/models.py:143`, `common-message.ts:114` | `c4ee9e2e` |
| `model_provider.api_version varchar(20)` | `app/data/models.py:208` | `cf116616` |
| `model_mappings` row for alias `deepseek-v4-flash` | `app/agent/reading.py:80` | — |

The last two went in together as mutation 255 on 2026-09-11. `api_version` pins the
API version segment the google-genai SDK appends to `base_url`; only
`client_type='google'` reads it (`app/agent/models.py:100`), and NULL keeps the SDK
default. Without the column, `app/data/queries/model_provider.py:43` selects the whole
entity and raises `UndefinedColumn` on the provider-resolution path
(`app/agent/models.py:84`) that every text and image call goes through — so the
failure is every model call, not just the Gemini ones. The `deepseek-v4-flash` alias
backs the reading agent; prod never had it, so that path was dead there.

Two constraints these carry, in case they are ever rebuilt:

- `mentioned_common_user_ids` must stay nullable with no default. NULL means the
  sender list was never computed; `{}` means it was computed and named nobody.
  `scripts/db/003-verify-common-layer.sql:20-26` asserts the distinction.
- The index name must keep the `ix_` prefix. `003-verify-common-layer.sql:38-48`
  asserts that column carries exactly one index under exactly that name.

`scripts/db/001-common-layer-schema.sql:56-63` and `:79-84` hold the same statements
and are safe to re-run. `api_version` is not in that script.

## What migrator covers

`app/runtime/migrator.py:167-280` creates a table per declared `Data` subclass
(skipping `AdminOnly`, `Meta.transient`, `Meta.existing_table`), appends `dedup_hash`
and `created_at`, and builds the dedup, version and `Meta.indexes` indexes.

The one precondition is CREATE privilege on the prod role. If it is missing,
`migrate_schema()` raises during the `app.main` lifespan and every agent-service pod
goes into CrashLoopBackoff — it does not degrade. Confirmed satisfied on 2026-09-11:
prod holds 31 `data_*` and `runtime_*` tables, which only migrator could have built
(`app/data/bootstrap.py:21` gates SQLAlchemy's `create_all` to `coe-*` lanes, so it
has never run on prod).

The living engine's twelve tables were created this way on the first startup after
release, confirmed 12/12 on 2026-09-12: `data_happening`, `data_whereabouts`,
`data_upcoming`, `data_life_moment`, `data_loose_end`, `data_spoken_outbound`,
`data_phone_read`, `data_world_round`, `data_living_day_page`, `data_picture`,
`data_file_read`, `data_file_picked_up`. prod now holds 43 `data_*` and 3 `runtime_*`
tables.

Migrator also fails the batch if a still-declared table has a column the class no
longer has (`migrator.py:218-224`) or if a column's pg type no longer matches the
declaration (`:232-238`). Both say "write explicit migration script" — meaning an
entry in this file. `data_happening` losing `lasts_until` takes the first path, and so
do the five location-based perception columns; both entries are under Pending. `data_persona_version` moved from `app/life/persona_chain.py` to
`app/living/persona.py:58` with its fields unchanged.

## Tables prod keeps but nothing reads

The branch deletes the classes behind these; the tables stay. No DDL, no drop — listed
only so a schema diff does not read as a discrepancy.

`data_act_performed`, `data_book_impression`, `data_chat_request`,
`data_common_message_content_synced`, `data_daily_materials`, `data_day_page`,
`data_event_envelope`, `data_event_read`, `data_jotting`, `data_jotting_watermark`,
`data_life_state`, `data_notebook_entry`, `data_reading_triggered`,
`data_relationship_page`.

The old world's tables used to be on this list; they are now scheduled for a drop
under Pending.

`data_day_page` is the reason the new page table is named `data_living_day_page`
(`6ad0e5a9`) — the old table is still there with a different shape.

## Not DDL, but the same trap

Langfuse prompts have no automatic mechanism either, and they fail the same way: a
prod process resolves a prompt with `label=None` (`app/agent/prompts.py:63`), the SDK
reads that as `production`, and a prompt that only carries a lane label 404s. A lane
never notices, because a lane falls back to `production` — so the gap is invisible
until the first prod deploy.

All five the living engine needs were labelled `production` on 2026-09-11:
`living_life_moment` (v8), `living_world_round`, `living_day_page`,
`living_persona_review`, `book_reading_impression`. `guard_output_safety` already had
one. When a new prompt id appears in an `AgentConfig`, label it before the deploy that
reads it. `living_world_round` no longer has a reader: the old world round that used it
is deleted.

## Running one

DDL goes through approval, never through a direct connection:

```
/ops-db submit @chiwei ALTER TABLE ... ;
-- reason: <why>
```

Then approve it in Dashboard → DB 变更. Re-query `information_schema` afterwards and
move the entry from Pending to Applied with the date.
