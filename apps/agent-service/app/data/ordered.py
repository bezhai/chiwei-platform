"""Records numbered in commit order.

Some records are read the way a cursor reads: everything numbered above the cursor, in order,
and then the cursor moves to the last number returned. A Data class declares such a number on
one field, with the fields that pick its axis
(``seq: Annotated[int, CommitOrder(within=("lane",))] = 0``, see
:class:`app.data.schema.CommitOrder`), and writes the record with :func:`insert_in_commit_order`.

It keeps three promises:

1. **Order.** A reader never sees a larger number while a smaller one on the same axis may still
   commit, whichever connection or process wrote them. Numbers are taken under a transaction-scoped
   advisory lock on the axis (:func:`app.data.persist.serialise_writers`), which the server
   releases only when the transaction has ended, after its commit is visible. The number is read
   in a later statement than the lock, and under READ COMMITTED each statement takes a new
   snapshot, so the next writer on the axis sees every earlier number committed or rolled back.
2. **Replay.** Writing a record whose Key is already stored inserts nothing, takes no number,
   and returns the stored record, read in the same transaction. The conflict is judged on
   ``dedup_hash``, which covers the Key fields only; the numbered field is never a Key.
3. **No half-written record.** The number is taken and the record inserted in one transaction:
   the caller's open ``tx()``, or one of its own. A failure or a cancellation before the commit
   rolls all of it back; the number was never visible and is handed out again.
"""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy import text

from app.data.persist import insert_idempotent, select_latest, serialise_writers
from app.data.schema import Data, commit_order, key_fields, table_name
from app.data.tx import auto_tx, current_session

D = TypeVar("D", bound=Data)


async def insert_in_commit_order(record: D) -> D:
    """Store ``record`` with the next number on its axis; return the record as stored.

    The numbered field's value on ``record`` is ignored: it gets ``MAX + 1`` among the stored
    records that agree with it on the axis fields. A replay (its Key already stored) returns the
    stored record, number and content, and writes nothing.

    Inside a caller's ``tx()`` it joins that transaction, so the record lands with the caller's
    other writes; several appends on one axis in one transaction get consecutive numbers.
    Preconditions:

    * The isolation level is READ COMMITTED, the engine's and Postgres's default. Under REPEATABLE
      READ a caller's transaction that has already read keeps its first snapshot, misses numbers
      committed since, and reuses one.
    * Inside a caller's transaction the axis stays locked until that transaction ends, so keep
      such transactions to database work, and short (``tx()`` warns past 5 s).
    * A wait for the axis is bounded by the server's ``statement_timeout`` and then fails with an
      error.
    """
    cls = type(record)
    declared = commit_order(cls)
    if declared is None:
        raise TypeError(f"{cls.__name__} declares no CommitOrder field")
    numbered, within = declared
    table = table_name(cls)
    axis = {name: getattr(record, name) for name in within}

    async with auto_tx():
        await serialise_writers(table, tuple(axis.values()))
        where = " AND ".join(f"{name} = :{name}" for name in within)
        result = await current_session().execute(
            text(f"SELECT COALESCE(MAX({numbered}), 0) + 1 FROM {table} WHERE {where}"),
            axis,
        )
        stored = record.model_copy(update={numbered: int(result.scalar_one())})
        if await insert_idempotent(stored) == 1:
            return stored
        existing = await select_latest(cls, {k: getattr(record, k) for k in key_fields(cls)})
    assert existing is not None, (
        f"{cls.__name__}: the insert met a stored record with the same Key, and reading that "
        f"Key found nothing"
    )
    return existing  # type: ignore[return-value]
