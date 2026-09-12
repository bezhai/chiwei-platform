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

Nothing.

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
entry in this file. No retained table changed shape on this branch, so neither path
fires; `data_persona_version` in particular moved from `app/life/persona_chain.py` to
`app/living/persona.py:58` with its fields unchanged.

## Tables prod keeps but nothing reads

The branch deletes the classes behind these; the tables stay. No DDL, no drop — listed
only so a schema diff does not read as a discrepancy.

`data_act_performed`, `data_book_impression`, `data_chat_request`,
`data_common_message_content_synced`, `data_daily_materials`, `data_day_page`,
`data_event_envelope`, `data_event_read`, `data_jotting`, `data_jotting_watermark`,
`data_life_state`, `data_notebook_entry`, `data_npc_roster`, `data_reading_triggered`,
`data_relationship_page`, `data_world_arc`, `data_world_attention`,
`data_world_outline`, `data_world_state`.

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
reads it.

## Running one

DDL goes through approval, never through a direct connection:

```
/ops-db submit @chiwei ALTER TABLE ... ;
-- reason: <why>
```

Then approve it in Dashboard → DB 变更. Re-query `information_schema` afterwards and
move the entry from Pending to Applied with the date.
