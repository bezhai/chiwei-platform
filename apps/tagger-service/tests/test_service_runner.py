from __future__ import annotations

import asyncio

import pytest

from app.service.runner import PersistentStageRunner


class FakeStage:
    def __init__(self) -> None:
        self.loads = 0
        self.unloads = 0
        self.runs = 0

    def load(self) -> None:
        self.loads += 1

    def run(self, items):
        self.runs += 1
        return {image_id: {"x": {"value": image}} for image_id, image in items}

    def unload(self) -> None:
        self.unloads += 1


class ExplodingStage(FakeStage):
    def run(self, items):
        self.runs += 1
        raise RuntimeError("stage blew up")


def test_persistent_runner_loads_once_for_consecutive_batches() -> None:
    async def scenario() -> None:
        stage = FakeStage()
        runner = PersistentStageRunner([stage])
        rows1, dups1 = await runner.run([("a", 1)])
        rows2, dups2 = await runner.run([("b", 2)])

        assert rows1[0]["x"]["value"] == 1
        assert rows2[0]["x"]["value"] == 2
        assert dups1 == []
        assert dups2 == []
        assert stage.loads == 1
        assert stage.runs == 2
        assert stage.unloads == 0

        await runner.unload()
        assert stage.unloads == 1

    asyncio.run(scenario())


def test_persistent_runner_dedups_before_stage_run() -> None:
    async def scenario() -> None:
        stage = FakeStage()
        runner = PersistentStageRunner([stage])
        rows, dups = await runner.run([("a", 1), ("a", 2)])

        assert [row["id"] for row in rows] == ["a"]
        assert rows[0]["x"]["value"] == 1
        assert dups == ["a"]

    asyncio.run(scenario())


def test_persistent_runner_unloads_stages_when_a_run_fails() -> None:
    async def scenario() -> None:
        stage = ExplodingStage()
        runner = PersistentStageRunner([stage])
        with pytest.raises(RuntimeError):
            await runner.run([("a", 1)])
        assert stage.unloads == 1

        # 卸载后下一批重新 load，不会带着半死状态复用
        await runner.unload()
        assert stage.unloads == 1

    asyncio.run(scenario())


def test_persistent_runner_unload_without_load_is_noop() -> None:
    async def scenario() -> None:
        stage = FakeStage()
        runner = PersistentStageRunner([stage])
        await runner.unload()
        assert stage.unloads == 0

    asyncio.run(scenario())
