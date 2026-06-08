from __future__ import annotations

import asyncio

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


def test_persistent_runner_loads_once_for_consecutive_batches() -> None:
    async def scenario() -> None:
        stage = FakeStage()
        runner = PersistentStageRunner([stage], idle_unload_seconds=None)
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


def test_persistent_runner_preload_loads_before_first_run() -> None:
    async def scenario() -> None:
        stage = FakeStage()
        runner = PersistentStageRunner([stage], idle_unload_seconds=None)

        await runner.preload()
        assert runner.loaded
        assert stage.loads == 1

        await runner.run([("a", 1)])
        assert stage.loads == 1
        assert stage.runs == 1

    asyncio.run(scenario())


def test_persistent_runner_idle_unloads_after_delay() -> None:
    async def scenario() -> None:
        stage = FakeStage()
        runner = PersistentStageRunner([stage], idle_unload_seconds=0.01)
        await runner.run([("a", 1)])
        assert runner.loaded
        await asyncio.sleep(0.03)
        assert not runner.loaded
        assert stage.unloads == 1

    asyncio.run(scenario())


def test_persistent_runner_dedups_before_stage_run() -> None:
    async def scenario() -> None:
        stage = FakeStage()
        runner = PersistentStageRunner([stage], idle_unload_seconds=None)
        rows, dups = await runner.run([("a", 1), ("a", 2)])

        assert [row["id"] for row in rows] == ["a"]
        assert rows[0]["x"]["value"] == 1
        assert dups == ["a"]

    asyncio.run(scenario())
