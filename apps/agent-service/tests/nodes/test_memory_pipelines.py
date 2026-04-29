import pytest
from unittest.mock import AsyncMock

from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
from app.nodes.memory_pipelines import (
    _LOCK_RELEASE_LUA, afterthought_check, drift_check,
)
from app.runtime.debounce import DebounceReschedule
from app.runtime.node import NODE_REGISTRY, _NODE_META


@pytest.fixture(autouse=True)
def _node_registry_isolation():
    nodes = set(NODE_REGISTRY)
    meta = dict(_NODE_META)
    yield
    NODE_REGISTRY.clear()
    NODE_REGISTRY.update(nodes)
    _NODE_META.clear()
    _NODE_META.update(meta)


@pytest.mark.asyncio
async def test_drift_check_lock_acquired_runs_run_drift_and_releases(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)  # SETNX 成功
    fake_redis.eval = AsyncMock(return_value=1)  # Lua release 删掉
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_run_drift = AsyncMock()
    monkeypatch.setattr("app.nodes.memory_pipelines._run_drift", fake_run_drift)

    await drift_check(DriftTrigger(chat_id="c1", persona_id="p1"))

    fake_run_drift.assert_awaited_once_with("c1", "p1")
    fake_redis.eval.assert_awaited_once()
    assert fake_redis.eval.call_args.args[0] == _LOCK_RELEASE_LUA


@pytest.mark.asyncio
async def test_drift_check_lock_busy_raises_debounce_reschedule(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=False)  # SETNX 失败
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_run_drift = AsyncMock()
    monkeypatch.setattr("app.nodes.memory_pipelines._run_drift", fake_run_drift)

    with pytest.raises(DebounceReschedule) as exc_info:
        await drift_check(DriftTrigger(chat_id="c1", persona_id="p1"))

    assert isinstance(exc_info.value.data, DriftTrigger)
    assert exc_info.value.data.chat_id == "c1"
    assert exc_info.value.data.persona_id == "p1"
    fake_run_drift.assert_not_awaited()
    fake_redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_drift_check_run_drift_raises_still_releases_lock(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.eval = AsyncMock(return_value=1)
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    monkeypatch.setattr("app.nodes.memory_pipelines._run_drift",
                        AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        await drift_check(DriftTrigger(chat_id="c1", persona_id="p1"))

    # finally 释放锁
    fake_redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_drift_check_release_uses_token_compare_and_delete(monkeypatch):
    """LLM 卡过 TTL 后旧 finally 不能误删新锁：Lua compare-and-delete
    在 token 不匹配时返回 0，不动 redis（reviewer round-2 H2）."""
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.eval = AsyncMock(return_value=0)  # token 不匹配 → no-op
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_run_drift = AsyncMock()
    monkeypatch.setattr("app.nodes.memory_pipelines._run_drift", fake_run_drift)

    await drift_check(DriftTrigger(chat_id="c1", persona_id="p1"))

    fake_redis.eval.assert_awaited_once()
    fake_run_drift.assert_awaited_once()


@pytest.mark.asyncio
async def test_afterthought_check_lock_busy_raises_debounce_reschedule(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=False)
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    monkeypatch.setattr("app.nodes.memory_pipelines._generate_fragment",
                        AsyncMock())

    with pytest.raises(DebounceReschedule) as exc_info:
        await afterthought_check(AfterthoughtTrigger(chat_id="c1", persona_id="p1"))

    assert isinstance(exc_info.value.data, AfterthoughtTrigger)
