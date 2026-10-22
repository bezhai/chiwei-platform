"""Declaring a record numbered in commit order: the ``CommitOrder`` marker.

A Data class names its numbered field and the axis it is numbered along on the field itself
(``seq: Annotated[int, CommitOrder(within=("lane",))] = 0``), and
:func:`app.data.ordered.insert_in_commit_order` reads both from there. Declarations that cannot
mean one axis are refused when the class is created, before it is registered, so a bad one never
reaches the migrator or a write.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import pytest

from app.data.migrator import plan_migration
from app.data.schema import (
    DATA_REGISTRY,
    CommitOrder,
    Data,
    Key,
    Version,
    commit_order,
    table_name,
)


class Numbered(Data):
    lane: Annotated[str, Key]
    item_id: Annotated[str, Key]
    seq: Annotated[int, CommitOrder(within=("lane",))] = 0
    body: str


class NumberedByHand(Data):
    """``Numbered`` as the four numbered records declare it before they move to the marker."""

    lane: Annotated[str, Key]
    item_id: Annotated[str, Key]
    seq: int
    body: str


class NumberedPerPerson(Data):
    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    item_id: Annotated[str, Key]
    seq: Annotated[int, CommitOrder(within=("lane", "persona_id"))] = 0


class Unnumbered(Data):
    item_id: Annotated[str, Key]


def test_commit_order_names_the_numbered_field_and_its_axis():
    assert commit_order(Numbered) == ("seq", ("lane",))
    assert commit_order(NumberedPerPerson) == ("seq", ("lane", "persona_id"))
    assert commit_order(Unnumbered) is None


def test_the_marker_and_its_default_change_no_ddl():
    """Moving a record onto the marker leaves its table exactly as it is."""

    def ddl(cls: type[Data]) -> list[str]:
        return [
            s.sql.replace(table_name(cls), "<table>")
            for s in plan_migration([cls], existing_schema={}).stmts
        ]

    assert ddl(Numbered) == ddl(NumberedByHand)


@contextmanager
def refused(match: str) -> Iterator[None]:
    """The class defined in the block is refused at creation, and not registered."""
    registered = set(DATA_REGISTRY)
    with pytest.raises(TypeError, match=match):
        yield
    assert set(DATA_REGISTRY) == registered


def test_a_second_numbered_field_is_refused():
    with refused("more than one CommitOrder field"):

        class TwoNumbers(Data):
            lane: Annotated[str, Key]
            item_id: Annotated[str, Key]
            seq: Annotated[int, CommitOrder(within=("lane",))] = 0
            other_seq: Annotated[int, CommitOrder(within=("lane",))] = 0


def test_a_numbered_field_that_is_also_a_key_is_refused():
    with refused("is also a Key"):

        class NumberIsKey(Data):
            lane: Annotated[str, Key]
            seq: Annotated[int, Key, CommitOrder(within=("lane",))] = 0


def test_an_axis_naming_an_undeclared_field_is_refused():
    with refused("not a field"):

        class AxisOnNothing(Data):
            lane: Annotated[str, Key]
            item_id: Annotated[str, Key]
            seq: Annotated[int, CommitOrder(within=("lane", "persona_id"))] = 0


def test_an_axis_naming_the_numbered_field_itself_is_refused():
    with refused("cannot be numbered along itself"):

        class AxisOnItself(Data):
            lane: Annotated[str, Key]
            item_id: Annotated[str, Key]
            seq: Annotated[int, CommitOrder(within=("lane", "seq"))] = 0


def test_an_axis_naming_no_field_is_refused():
    with refused("names no field"):

        class AxisOfNothing(Data):
            item_id: Annotated[str, Key]
            seq: Annotated[int, CommitOrder(within=())] = 0


def test_a_numbered_class_with_a_version_is_refused():
    with refused("both CommitOrder and Version"):

        class NumberedAndVersioned(Data):
            lane: Annotated[str, Key]
            item_id: Annotated[str, Key]
            ver: Annotated[int, Version] = 0
            seq: Annotated[int, CommitOrder(within=("lane",))] = 0
