from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.domain.memory_triggers import AfterthoughtTrigger
from app.nodes.memory_pipelines import afterthought_check
from app.runtime.debounce import DebounceReschedule
from app.runtime.node import _NODE_META, NODE_REGISTRY
from app.runtime.single_flight import SingleFlightConflict


@pytest.fixture(autouse=True)
def _node_registry_isolation():
    nodes = set(NODE_REGISTRY)
    meta = dict(_NODE_META)
    yield
    NODE_REGISTRY.clear()
    NODE_REGISTRY.update(nodes)
    _NODE_META.clear()
    _NODE_META.update(meta)


def _patch_single_flight(monkeypatch, target_module: str, *, conflict: bool):
    """Replace single_flight in `target_module` with a fake CM.

    conflict=True → raises SingleFlightConflict on entry.
    conflict=False → yields normally.
    """
    captured: dict = {}

    @asynccontextmanager
    async def _fake(key, *, ttl):
        captured["key"] = key
        captured["ttl"] = ttl
        if conflict:
            raise SingleFlightConflict(key)
        yield

    monkeypatch.setattr(f"{target_module}.single_flight", _fake)
    return captured


def test_drift_pipeline_gone():
    """voice 子系统拆除：drift（voice 再生成）整条管线不得残留。"""
    import app.nodes.memory_pipelines as mp

    for name in ("drift_check", "_run_drift", "_recent_persona_replies",
                 "_recent_timeline"):
        assert not hasattr(mp, name), f"{name} should have been deleted"


@pytest.mark.asyncio
async def test_afterthought_check_lock_busy_raises_debounce_reschedule(monkeypatch):
    _patch_single_flight(monkeypatch, "app.nodes.memory_pipelines", conflict=True)
    monkeypatch.setattr(
        "app.nodes.memory_pipelines._generate_fragment", AsyncMock()
    )

    with pytest.raises(DebounceReschedule) as exc_info:
        await afterthought_check(AfterthoughtTrigger(chat_id="c1", persona_id="p1"))

    assert isinstance(exc_info.value.data, AfterthoughtTrigger)


@pytest.mark.asyncio
async def test_afterthought_check_lock_acquired_uses_900s_ttl(monkeypatch):
    captured = _patch_single_flight(
        monkeypatch, "app.nodes.memory_pipelines", conflict=False
    )
    fake_gen = AsyncMock()
    monkeypatch.setattr("app.nodes.memory_pipelines._generate_fragment", fake_gen)

    await afterthought_check(AfterthoughtTrigger(chat_id="c1", persona_id="p1"))

    fake_gen.assert_awaited_once_with("c1", "p1")
    assert captured["key"] == "phase2:afterthought:c1:p1"
    assert captured["ttl"] == 900


@pytest.mark.asyncio
async def test_afterthought_check_generate_raises_propagates(monkeypatch):
    _patch_single_flight(monkeypatch, "app.nodes.memory_pipelines", conflict=False)
    monkeypatch.setattr(
        "app.nodes.memory_pipelines._generate_fragment",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await afterthought_check(AfterthoughtTrigger(chat_id="c1", persona_id="p1"))
