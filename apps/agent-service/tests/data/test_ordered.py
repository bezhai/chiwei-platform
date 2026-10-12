"""Records numbered in commit order: :func:`app.data.ordered.insert_in_commit_order`.

Her readers read a numbered record the way a cursor does: everything numbered above the cursor,
in order, and then the cursor moves to the last number returned. That only works if the storage
keeps three promises, each pinned here on a real Postgres:

1. **Order.** A reader never sees a larger number while a smaller one may still commit, whichever
   task or process wrote them; otherwise the cursor moves past the smaller one and it is never
   read.
2. **Replay.** Writing the same record again (same Key) stores nothing and returns the record
   that is stored.
3. **No half-written record.** A failure or a cancellation leaves nothing visible and does not hold
   up the next writer on the axis.

``python -m tests.data.test_ordered <lane> <count>`` is the second process of
:func:`test_numbers_follow_commit_order_across_processes`.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import random
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Annotated

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from app.data.ordered import insert_in_commit_order
from app.data.persist import insert_idempotent
from app.data.schema import CommitOrder, Data, Key, table_name
from app.data.tx import current_session, tx
from tests.runtime.conftest import migrate

pytestmark = pytest.mark.integration

# How soon the next writer on an axis must get through after the previous one failed or was
# cancelled. A lock left behind would hold it until the connection goes away: for ever, on the
# test engine.
PROMPTLY = 1.0


class Probe(Data):
    lane: Annotated[str, Key]
    probe_id: Annotated[str, Key]
    seq: Annotated[int, CommitOrder(within=("lane",))] = 0
    writer: str


class ProbeNote(Data):
    """A row of another table, written in the same transaction as probes."""

    note_id: Annotated[str, Key]


@pytest.fixture
async def probes(real_pg_required, test_db):  # noqa: F811
    await migrate(Probe, test_db)
    await migrate(ProbeNote, test_db)
    yield test_db


def _probe(probe_id: str, *, lane: str = "L", writer: str = "w") -> Probe:
    return Probe(lane=lane, probe_id=probe_id, writer=writer)


async def _visible(engine, *, lane: str = "L", after: int = 0) -> list[tuple[int, str]]:
    """``(seq, probe_id)`` above ``after`` on ``lane``, in order, as another connection sees them."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                f"SELECT seq, probe_id FROM {table_name(Probe)} "
                "WHERE lane = :lane AND seq > :after ORDER BY seq"
            ),
            {"lane": lane, "after": after},
        )
        return [(seq, probe_id) for seq, probe_id in rows]


async def _notes(engine) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table_name(ProbeNote)}"))
        ).scalar_one()


class CursorReader:
    """Reads one axis the way her readers do: what is above the cursor, in order; then the cursor
    moves to the last number returned."""

    def __init__(self, engine, lane: str) -> None:
        self.engine = engine
        self.lane = lane
        self.cursor = 0
        self.got: list[tuple[int, str]] = []

    async def read(self) -> None:
        rows = await _visible(self.engine, lane=self.lane, after=self.cursor)
        if rows:
            self.cursor = rows[-1][0]
            self.got.extend(rows)


async def _read_while(work: Awaitable[object], readers: list[CursorReader]) -> None:
    """Keep every reader reading until ``work`` is done, then read once more."""
    work = asyncio.ensure_future(work)
    while not work.done():
        for reader in readers:
            await reader.read()
        await asyncio.sleep(0.001)
    await work
    for reader in readers:
        await reader.read()


async def _assert_each_row_read_once(reader: CursorReader, *, rows: int) -> list[tuple[int, str]]:
    stored = await _visible(reader.engine, lane=reader.lane)
    assert [seq for seq, _ in stored] == list(range(1, rows + 1)), (
        f"axis {reader.lane} is not numbered 1..{rows} without duplicates: {stored}"
    )
    assert reader.got == stored, (
        f"the cursor reader on {reader.lane} skipped "
        f"{sorted(set(stored) - set(reader.got))}: a larger number became visible while a "
        f"smaller one had not committed yet"
    )
    return stored


async def _append_many(lane: str, *, writer: str, count: int) -> None:
    """``count`` appends, each in its own ``tx()`` that stays open 0-10 ms after the append: the
    window in which the number is taken but not yet committed."""
    for i in range(count):
        async with tx():
            await insert_in_commit_order(_probe(f"{writer}-{i}", lane=lane, writer=writer))
            await asyncio.sleep(random.uniform(0, 0.01))


# ---------------------------------------------------------------------------
# 1 · Order
# ---------------------------------------------------------------------------


async def _until_blocked_or_done(engine, task: asyncio.Task, *, at_most: float = 1.0) -> None:
    """Return once ``task`` is done, or a transaction waits on a lock in Postgres, or ``at_most``
    seconds have passed. The time bound gives a writer that waits some other way the same chance
    to become visible, so what the caller asserts is visibility, not how the writer was held."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + at_most
    while not task.done() and loop.time() < deadline:
        async with engine.connect() as conn:
            waiting = (
                await conn.execute(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
            ).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.01)


async def test_a_later_number_is_not_visible_while_an_earlier_one_can_still_commit(probes):
    appended: asyncio.Future[Probe] = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def writer_a() -> Probe:
        async with tx():
            a = await insert_in_commit_order(_probe("a", writer="A"))
            appended.set_result(a)
            await release.wait()
        return a

    task_a = asyncio.create_task(writer_a())
    task_b: asyncio.Task | None = None
    try:
        n = (await appended).seq
        task_b = asyncio.create_task(insert_in_commit_order(_probe("b", writer="B")))
        await _until_blocked_or_done(probes, task_b)
        assert await _visible(probes, after=n - 1) == [], (
            f"while A had taken {n} and not committed, a reader saw a number from {n} on: "
            f"B's append became visible ahead of A's"
        )
    finally:
        release.set()
        a = await task_a
        b = await task_b if task_b is not None else None

    assert b is not None
    assert (a.seq, b.seq) == (n, n + 1)
    assert await _visible(probes, after=n - 1) == [(n, "a"), (n + 1, "b")]


async def test_concurrent_writers_and_a_cursor_reader_never_skip(probes):
    lanes = ("L1", "L2")
    readers = [CursorReader(probes, lane) for lane in lanes]

    async def writer(w: int) -> None:
        for i in range(25):
            async with tx():
                await insert_in_commit_order(
                    _probe(f"{w}-{i}", lane=lanes[(w + i) % 2], writer=str(w))
                )
                await asyncio.sleep(random.uniform(0, 0.01))

    await _read_while(asyncio.gather(*(writer(w) for w in range(8))), readers)

    for reader in readers:
        await _assert_each_row_read_once(reader, rows=100)


async def test_numbers_follow_commit_order_across_processes(probes, test_db_dsn, tmp_path):
    """The only test that tells a lock held by the database from a lock held by the process: two
    processes write one axis at once, as an old and a new pod overlap during a rolling update."""
    from sqlalchemy.engine import make_url

    from tests.hosting import SERVICE_ROOT

    url = make_url(test_db_dsn)
    env = {
        **os.environ,
        "POSTGRES_HOST": url.host,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_DB": url.database,
    }
    errors = tmp_path / "child.stderr"
    with errors.open("wb") as err:
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.data.test_ordered", "L", "50",
            cwd=SERVICE_ROOT, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=err,
        )
    try:
        async with asyncio.timeout(120):
            ready = await child.stdout.readline()
        assert ready.strip() == b"ready", errors.read_text()
        reader = CursorReader(probes, "L")
        child.stdin.write(b"go\n")
        await child.stdin.drain()
        await _read_while(
            asyncio.gather(_append_many("L", writer="parent", count=50), child.wait()),
            [reader],
        )
        assert child.returncode == 0, errors.read_text()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()

    stored = await _assert_each_row_read_once(reader, rows=100)
    by_writer = [probe_id.split("-")[0] for _, probe_id in stored]
    turns = sum(1 for x, y in zip(by_writer, by_writer[1:], strict=False) if x != y)
    assert turns > 1, f"the two processes did not write at the same time: {by_writer}"


@pytest.fixture
async def on_the_production_engine(probes, test_db_dsn, monkeypatch):
    """Storage on an engine built the way production builds it
    (:func:`app.data.session.make_engine`), pointed at the test database."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import app.data.session as session_mod

    engine = session_mod.make_engine(make_url(test_db_dsn))
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(
        session_mod,
        "async_session",
        async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False),
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_an_append_in_a_transaction_that_already_read_sees_the_last_commit(
    on_the_production_engine,
):
    """Pins READ COMMITTED: the number is read after the lock, in a statement of its own, so it
    sees every commit made before the lock was granted, even in a transaction that read before.
    Under REPEATABLE READ that transaction would keep its first snapshot and reuse a number."""
    async with tx():
        await current_session().execute(text(f"SELECT count(*) FROM {table_name(Probe)}"))
        # Another writer, outside this transaction (a fresh context, so it does not join it).
        other = await asyncio.create_task(
            insert_in_commit_order(_probe("other")), context=contextvars.Context()
        )
        mine = await insert_in_commit_order(_probe("mine"))

    assert (other.seq, mine.seq) == (1, 2)


# ---------------------------------------------------------------------------
# 2 · Replay
# ---------------------------------------------------------------------------


async def test_a_replay_returns_the_stored_record_and_writes_nothing(probes):
    first = await insert_in_commit_order(_probe("p", writer="X"))
    replay = await insert_in_commit_order(_probe("p", writer="Y"))

    assert replay == first
    assert (replay.seq, replay.writer) == (1, "X")
    assert await _visible(probes) == [(1, "p")]
    assert (await insert_in_commit_order(_probe("q"))).seq == 2, "the replay took no number"


async def test_concurrent_replays_store_one_record(probes):
    a, b = await asyncio.gather(
        insert_in_commit_order(_probe("p", writer="X")),
        insert_in_commit_order(_probe("p", writer="Y")),
    )

    assert a == b
    assert await _visible(probes) == [(1, "p")]


async def test_a_replay_inside_the_callers_transaction_does_not_abort_it(probes):
    first = await insert_in_commit_order(_probe("p", writer="X"))

    async with tx():
        replay = await insert_in_commit_order(_probe("p", writer="Y"))
        later = await insert_in_commit_order(_probe("q"))
        await insert_idempotent(ProbeNote(note_id="n"))

    assert replay == first
    assert later.seq == 2
    assert await _visible(probes) == [(1, "p"), (2, "q")]
    assert await _notes(probes) == 1


# ---------------------------------------------------------------------------
# 3 · No half-written record
# ---------------------------------------------------------------------------


async def _the_axis_is_free() -> None:
    async with asyncio.timeout(PROMPTLY):
        nxt = await insert_in_commit_order(_probe("next"))
    assert nxt.seq == 1, "the number of the record that never landed is handed out again"


async def test_a_failed_transaction_leaves_no_record_and_frees_the_axis(probes):
    with pytest.raises(RuntimeError, match="after the append"):
        async with tx():
            await insert_in_commit_order(_probe("p"))
            raise RuntimeError("the caller failed after the append")

    assert await _visible(probes) == []
    await _the_axis_is_free()


@contextmanager
def commits_failing(engine) -> Iterator[Callable[[], None]]:
    """Yields ``arm``: once called, the next COMMIT on ``engine`` fails the way the driver fails
    one (as ``tests/replay/harness/database.py`` injects it), and the transaction is rolled back."""
    armed: list[bool] = []

    def fail(conn) -> None:
        if armed:
            armed.clear()
            raise OperationalError("COMMIT", {}, ConnectionError("injected commit failure"))

    event.listen(engine.sync_engine, "commit", fail)
    try:
        yield lambda: armed.append(True)
    finally:
        event.remove(engine.sync_engine, "commit", fail)


async def test_a_failure_at_commit_leaves_nothing(probes):
    with commits_failing(probes) as arm:
        with pytest.raises(OperationalError, match="injected commit failure"):
            async with tx():
                await insert_in_commit_order(_probe("p"))
                arm()  # this transaction's COMMIT is the next one

    assert await _visible(probes) == []
    await _the_axis_is_free()


async def test_a_cancelled_writer_leaves_nothing_and_does_not_block_the_next(probes):
    appended = asyncio.Event()

    async def writer() -> None:
        async with tx():
            await insert_in_commit_order(_probe("p"))
            appended.set()
            await asyncio.Event().wait()  # for ever

    task = asyncio.create_task(writer())
    await appended.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _visible(probes) == []
    await _the_axis_is_free()


async def test_records_written_together_land_together(probes):
    with pytest.raises(RuntimeError, match="at the end"):
        async with tx():
            await insert_in_commit_order(_probe("a"))
            await insert_in_commit_order(_probe("b"))
            await insert_idempotent(ProbeNote(note_id="n"))
            raise RuntimeError("the caller failed at the end")

    assert await _visible(probes) == []
    assert await _notes(probes) == 0

    async with tx():
        a = await insert_in_commit_order(_probe("a"))
        b = await insert_in_commit_order(_probe("b"))
        await insert_idempotent(ProbeNote(note_id="n"))

    assert (a.seq, b.seq) == (1, 2)
    assert await _visible(probes) == [(1, "a"), (2, "b")]
    assert await _notes(probes) == 1


# ---------------------------------------------------------------------------
# The second process of the two-process test
# ---------------------------------------------------------------------------


async def _write_from_another_process(lane: str, count: int) -> None:
    """Connects, prints ``ready``, waits for a line on stdin, then appends ``count`` probes."""
    import app.data.session as session_mod

    async with tx():
        await current_session().execute(text("SELECT 1"))
    print("ready", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await _append_many(lane, writer="child", count=count)
    await session_mod.engine.dispose()


if __name__ == "__main__":
    asyncio.run(_write_from_another_process(sys.argv[1], int(sys.argv[2])))
