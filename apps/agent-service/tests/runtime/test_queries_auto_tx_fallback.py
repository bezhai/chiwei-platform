"""A query function's auto_tx fallback works without an explicit tx().

Phase 7d Gap 13 — ``app.data.queries.persona.find_persona`` called from
outside any ``async with tx():`` block opens its own transaction via
``auto_tx`` to satisfy ``current_session()``, and does not leave it open.
Inside a caller's ``tx()`` it uses that transaction's session (it finds a
row the caller has added and not committed).

This used to be a documentation-only skip, waiting for business callers to
reach these queries without a session of their own. Since then nothing
outside ``app.data`` opens a session: every read goes through this
fallback, so it is pinned here on a real ``bot_persona`` table.
"""
from __future__ import annotations

import pytest

from app.data.models import Base, BotPersona
from app.data.queries.persona import find_persona
from app.data.tx import current_session, tx

pytestmark = pytest.mark.integration


def _persona(persona_id: str) -> BotPersona:
    return BotPersona(
        persona_id=persona_id,
        display_name="赤尾",
        persona_core="",
        persona_lite="",
        default_reply_style="",
        error_messages={},
    )


@pytest.fixture
async def persona_db(real_pg_required, test_db):
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[BotPersona.__table__])
        )
    yield test_db


async def test_a_query_outside_tx_opens_and_closes_its_own_transaction(persona_db):
    async with tx():
        current_session().add(_persona("akao"))

    found = await find_persona("akao")

    assert found is not None and found.display_name == "赤尾"
    assert await find_persona("nobody") is None
    with pytest.raises(RuntimeError, match="outside tx"):
        current_session()


async def test_a_query_inside_tx_reads_the_callers_transaction(persona_db):
    async with tx():
        current_session().add(_persona("ayana"))
        await current_session().flush()
        found = await find_persona("ayana")

    assert found is not None and found.persona_id == "ayana"
