"""Test recall_engine run_recall() pure function."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory.recall_engine import RecallResult, run_recall


@dataclass
class _FakeAbstract:
    id: str
    subject: str = "user:u1"
    content: str = "abstract content"
    clarity: str = "clear"


@dataclass
class _FakeFragment:
    id: str
    content: str = "fragment content"
    clarity: str = "clear"


@dataclass
class _FakeEdge:
    from_id: str
    from_type: str = "fact"
    edge_type: str = "supports"


@dataclass
class _RecallMocks:
    """Handles for a full set of patched collaborators.

    Tests mutate these (e.g. ``mocks.abstract_ids = ["a1", "a2"]``) to shape
    the scenario, then call ``run_recall`` and inspect the resulting mocks.
    """

    # Scenario inputs — tests set these before calling run_recall.
    abstract_ids_per_query: list[list[str]] = field(default_factory=list)
    fragment_ids_per_query: list[list[str]] = field(default_factory=list)
    abstracts_by_id: dict[str, _FakeAbstract | None] = field(default_factory=dict)
    fragments_by_id: dict[str, _FakeFragment] = field(default_factory=dict)
    edges_by_abstract: dict[str, list[_FakeEdge]] = field(default_factory=dict)

    # Mock handles — tests assert against these after calling run_recall.
    embed_dense: AsyncMock = None  # type: ignore[assignment]
    qdrant: MagicMock = None  # type: ignore[assignment]
    get_abstract_by_id: AsyncMock = None  # type: ignore[assignment]
    list_edges_to: AsyncMock = None  # type: ignore[assignment]
    get_fragments_by_ids: AsyncMock = None  # type: ignore[assignment]
    touch_abstracts_bulk: AsyncMock = None  # type: ignore[assignment]
    touch_fragments_bulk: AsyncMock = None  # type: ignore[assignment]


@pytest.fixture
def recall_mocks(monkeypatch):
    """Patch every collaborator of recall_engine and return a handle."""
    import app.memory.recall_engine as re_mod

    state = _RecallMocks()

    # Embed: deterministic vector.
    state.embed_dense = AsyncMock(return_value=[0.1] * 1024)
    monkeypatch.setattr(re_mod, "embed_dense", state.embed_dense)

    # Qdrant: per-call-site sequential responses driven by the lists above.
    qdrant_mock = MagicMock()
    abs_calls = {"n": 0}
    frag_calls = {"n": 0}

    async def fake_query_points(**kwargs):
        collection = kwargs.get("collection_name")
        if collection == re_mod.COLLECTION_ABSTRACT:
            idx = abs_calls["n"]
            abs_calls["n"] += 1
            ids = (
                state.abstract_ids_per_query[idx]
                if idx < len(state.abstract_ids_per_query)
                else []
            )
        else:
            idx = frag_calls["n"]
            frag_calls["n"] += 1
            ids = (
                state.fragment_ids_per_query[idx]
                if idx < len(state.fragment_ids_per_query)
                else []
            )
        return MagicMock(points=[MagicMock(id=x) for x in ids])

    qdrant_mock.client.query_points = AsyncMock(side_effect=fake_query_points)
    state.qdrant = qdrant_mock
    monkeypatch.setattr(re_mod, "qdrant", qdrant_mock)

    # get_session: yields a sentinel session — none of the patched query
    # helpers care about the argument.
    @asynccontextmanager
    async def fake_get_session():
        yield MagicMock(name="session")

    monkeypatch.setattr(re_mod, "get_session", fake_get_session)

    # Query helpers: read from the scenario dicts.
    async def fake_get_abstract_by_id(_session, aid):
        return state.abstracts_by_id.get(aid)

    state.get_abstract_by_id = AsyncMock(side_effect=fake_get_abstract_by_id)
    monkeypatch.setattr(re_mod, "get_abstract_by_id", state.get_abstract_by_id)

    async def fake_list_edges_to(_session, *, persona_id, to_id, edge_type):
        return state.edges_by_abstract.get(to_id, [])

    state.list_edges_to = AsyncMock(side_effect=fake_list_edges_to)
    monkeypatch.setattr(re_mod, "list_edges_to", state.list_edges_to)

    async def fake_get_fragments_by_ids(_session, ids):
        return [state.fragments_by_id[i] for i in ids if i in state.fragments_by_id]

    state.get_fragments_by_ids = AsyncMock(side_effect=fake_get_fragments_by_ids)
    monkeypatch.setattr(re_mod, "get_fragments_by_ids", state.get_fragments_by_ids)

    state.touch_abstracts_bulk = AsyncMock()
    monkeypatch.setattr(re_mod, "touch_abstracts_bulk", state.touch_abstracts_bulk)

    state.touch_fragments_bulk = AsyncMock()
    monkeypatch.setattr(re_mod, "touch_fragments_bulk", state.touch_fragments_bulk)

    return state


@pytest.mark.asyncio
async def test_run_recall_returns_abstracts_with_supporting_facts(recall_mocks):
    recall_mocks.abstract_ids_per_query = [["a_1"]]
    recall_mocks.abstracts_by_id = {"a_1": _FakeAbstract(id="a_1", content="他是程序员")}
    recall_mocks.edges_by_abstract = {"a_1": [_FakeEdge(from_id="f_1")]}
    recall_mocks.fragments_by_id = {"f_1": _FakeFragment(id="f_1", content="他说他在写 Rust")}

    result = await run_recall(
        persona_id="chiwei",
        queries=["浩南"],
        k_abs=5,
        k_facts_per_abs=3,
    )

    assert isinstance(result, RecallResult)
    assert len(result.abstracts) == 1
    assert result.abstracts[0]["id"] == "a_1"
    assert len(result.abstracts[0]["supporting_facts"]) == 1
    assert result.abstracts[0]["supporting_facts"][0]["id"] == "f_1"


@pytest.mark.asyncio
async def test_run_recall_filters_forgotten(recall_mocks):
    # Abstract search returns nothing => no abstracts.
    recall_mocks.abstract_ids_per_query = [[]]

    result = await run_recall(
        persona_id="chiwei", queries=["x"], k_abs=5, k_facts_per_abs=3,
    )

    assert result.abstracts == []


@pytest.mark.asyncio
async def test_run_recall_empty_query_list_returns_empty():
    result = await run_recall(persona_id="chiwei", queries=[], k_abs=5, k_facts_per_abs=3)
    assert result.abstracts == []


@pytest.mark.asyncio
async def test_run_recall_skips_forgotten_abstract(recall_mocks):
    recall_mocks.abstract_ids_per_query = [["a_forgot", "a_clear"]]
    recall_mocks.abstracts_by_id = {
        "a_forgot": _FakeAbstract(id="a_forgot", clarity="forgotten"),
        "a_clear": _FakeAbstract(id="a_clear"),
    }

    result = await run_recall(
        persona_id="chiwei", queries=["x"], k_abs=5, k_facts_per_abs=3,
    )

    assert len(result.abstracts) == 1
    assert result.abstracts[0]["id"] == "a_clear"


@pytest.mark.asyncio
async def test_run_recall_cross_query_dedup(recall_mocks):
    # Two queries both surface the same abstract id.
    recall_mocks.abstract_ids_per_query = [["a_1"], ["a_1"]]
    recall_mocks.abstracts_by_id = {"a_1": _FakeAbstract(id="a_1")}
    recall_mocks.edges_by_abstract = {"a_1": []}

    result = await run_recall(
        persona_id="chiwei",
        queries=["q1", "q2"],
        k_abs=5,
        k_facts_per_abs=3,
    )

    assert len(result.abstracts) == 1
    recall_mocks.touch_abstracts_bulk.assert_awaited_once()
    ids_arg = recall_mocks.touch_abstracts_bulk.await_args.args[1]
    assert sorted(ids_arg) == ["a_1"]


@pytest.mark.asyncio
async def test_run_recall_also_search_facts_path(recall_mocks):
    # Abstract search empty; fragment search returns fact ids.
    recall_mocks.abstract_ids_per_query = [[]]
    recall_mocks.fragment_ids_per_query = [["f_1", "f_2"]]
    recall_mocks.fragments_by_id = {
        "f_1": _FakeFragment(id="f_1"),
        "f_2": _FakeFragment(id="f_2"),
    }

    result = await run_recall(
        persona_id="chiwei",
        queries=["x"],
        k_abs=5,
        k_facts_per_abs=3,
        also_search_facts=True,
    )

    assert result.abstracts == []
    assert sorted(f["id"] for f in result.facts) == ["f_1", "f_2"]


@pytest.mark.asyncio
async def test_run_recall_also_search_facts_skips_already_supporting(recall_mocks):
    # Abstract A supports fragment F via its edges; fragment search also
    # surfaces F — F must only appear inside abstracts[0].supporting_facts,
    # never in result.facts.
    recall_mocks.abstract_ids_per_query = [["a_1"]]
    recall_mocks.fragment_ids_per_query = [["f_1"]]
    recall_mocks.abstracts_by_id = {"a_1": _FakeAbstract(id="a_1")}
    recall_mocks.edges_by_abstract = {"a_1": [_FakeEdge(from_id="f_1")]}
    recall_mocks.fragments_by_id = {"f_1": _FakeFragment(id="f_1")}

    result = await run_recall(
        persona_id="chiwei",
        queries=["x"],
        k_abs=5,
        k_facts_per_abs=3,
        also_search_facts=True,
    )

    assert len(result.abstracts) == 1
    assert [f["id"] for f in result.abstracts[0]["supporting_facts"]] == ["f_1"]
    assert result.facts == []


@pytest.mark.asyncio
async def test_run_recall_calls_touch_bulk(recall_mocks):
    recall_mocks.abstract_ids_per_query = [["a_1", "a_1"]]  # duplicate inside one query
    recall_mocks.abstracts_by_id = {"a_1": _FakeAbstract(id="a_1")}
    recall_mocks.edges_by_abstract = {"a_1": [_FakeEdge(from_id="f_1")]}
    recall_mocks.fragments_by_id = {"f_1": _FakeFragment(id="f_1")}

    await run_recall(
        persona_id="chiwei", queries=["q"], k_abs=5, k_facts_per_abs=3,
    )

    recall_mocks.touch_abstracts_bulk.assert_awaited_once()
    recall_mocks.touch_fragments_bulk.assert_awaited_once()

    abs_ids = recall_mocks.touch_abstracts_bulk.await_args.args[1]
    frag_ids = recall_mocks.touch_fragments_bulk.await_args.args[1]
    assert sorted(abs_ids) == ["a_1"]
    assert sorted(frag_ids) == ["f_1"]
