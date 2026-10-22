"""The scenario-facing object: the boundaries installed, the app process, and the steps.

A scenario (one test) starts an app, seeds whatever the rounds read, scripts the model, runs
steps, and checks the result against its baseline::

    await replay.start("agent-service")
    replay.model.script("living_life_moment", Reply(...), ...)
    await replay.step("first moment", lambda: replay.wake("akao", "regular"), at=datetime(...))
    replay.check("living_moment/continuation")

Everything a step does is recorded (see :mod:`tests.replay.harness.baseline` for the layout);
anything done between steps (seeding, scripting) is not. A step that is expected to fail names
the exception type with ``raises=``; any other exception fails the test.
"""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import time_machine
from pydantic import BaseModel

from app.infra.cst_time import now_cst
from tests.replay.harness import (
    baseline,
    image_model,
    model,
    prompts,
    volume,
    weather,
    web,
)
from tests.replay.harness.broker import FakeBroker
from tests.replay.harness.database import Tables, WriteLog, create_schema, row_changes
from tests.replay.harness.errors import describe_error
from tests.replay.harness.ids import DeterministicIds
from tests.replay.harness.image_model import ImageModel
from tests.replay.harness.network import Network, point_providers_at
from tests.replay.harness.objects import ObjectStore
from tests.replay.harness.process import AppProcess
from tests.replay.harness.sandbox import Sandbox
from tests.replay.harness.timeline import Timeline
from tests.replay.harness.volume import file_changes, read_tree
from tests.replay.harness.weather import Weather

CONTAINER_TZ = "Asia/Shanghai"


class _TracingOff(RuntimeError):
    pass


def _no_langfuse():
    raise _TracingOff("replay: tracing is not part of the baseline")


def _describe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _describe(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_describe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _describe(v) for k, v in value.items()}
    return repr(value)


class Replay:
    def __init__(
        self, *, engine, monkeypatch, root: Path, lane: str, start: datetime
    ) -> None:
        self.lane = lane
        # Everything with an order that matters, in the order it happened during a step: write
        # transactions ending, publishes, deliveries and how they settled, file writes/deletes.
        self.effects = Timeline()
        # Every HTTP request; hosts nothing serves are refused (see harness/network.py).
        self.network = Network(self.effects)
        # Object storage and tool-service's image pipeline (attachment and picture bytes).
        self.objects = ObjectStore(self.network)
        # The outside services her and world's tools reach: what each answers is given by the
        # scenario, and a request it gave no answer for is refused.
        self.web = web.Web(self.network)
        self.image_model = ImageModel(self.network)
        self.sandbox = Sandbox(self.network)
        self.weather = Weather(self.network)
        self.model = model.ModelScript(self.effects, objects=self.objects)
        self.langfuse = prompts.FixtureLangfuse()
        self.broker = FakeBroker(self.effects)
        self.ids = DeterministicIds()
        # Dynamic Config as this scenario sees it: key -> raw string value. Unset keys read as
        # unset (the code's own defaults apply).
        self.config: dict[str, str] = {}
        self.volume = root / "world-volume"
        # The skills plugin loads ``SKILLS_DIR`` when it is set up: empty, no guides on hand,
        # unless the scenario puts one there before starting the process (:meth:`guide`).
        self._skills = root / "skills"
        self._restore_settings = None
        self._engine = engine
        self._monkeypatch = monkeypatch
        self._start = start
        self._clock = None
        self._frozen = None
        self._tz_before: str | None = None
        self._writes: WriteLog | None = None
        self._tables = Tables(engine)
        self.redis = None
        self.process: AppProcess | None = None
        # Processes started so far; names each one's messaging worker (``<app>#<n>``).
        self._started = 0
        self.steps: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        mp = self._monkeypatch
        mp.setenv("LANE", self.lane)
        mp.setenv("WORLD_DATA_DIR", str(self.volume))
        mp.setenv("SKILLS_DIR", str(self._skills))
        mp.delenv("RABBITMQ_DISABLE_DELAYED", raising=False)
        # The container's zone (Dockerfile ``ENV TZ``), so naive local time reads the same on
        # every machine the replay runs on.
        self._tz_before = os.environ.get("TZ")
        os.environ["TZ"] = CONTAINER_TZ
        time.tzset()
        self.volume.mkdir(parents=True, exist_ok=True)
        self._skills.mkdir(parents=True, exist_ok=True)

        # time-machine, not freezegun: freezegun swaps ``datetime.date`` / ``datetime.datetime``
        # for its own subclasses, which changes code that compares types (the migrator and the
        # persist path map ``t is datetime.date`` to a column type) and that asyncpg will not
        # encode. time-machine moves the clock under the real classes. Only wall-clock reads
        # are frozen; the event loop's monotonic clock runs normally.
        self._clock = time_machine.travel(self._start, tick=False)
        self._frozen = self._clock.start()
        self.ids.install(mp)
        model.install(self.model, mp)
        self.broker.install(mp)
        self.network.install(mp)
        image_model.install(mp)
        self._restore_settings = point_providers_at(
            mp, {**web.SETTINGS, **weather.SETTINGS}
        )
        volume.install(mp, self.volume, self.effects)
        self._install_langfuse(mp)
        self._install_dynamic_config(mp)
        await self._install_redis(mp)

        await create_schema(self._engine)
        self._writes = WriteLog(self._engine, self.effects)

    async def close(self) -> None:
        try:
            if self.process is not None:
                await self.process.stop()
        finally:
            if self._writes is not None:
                self._writes.close()
            if self._restore_settings is not None:
                self._restore_settings()
            if self._clock is not None:
                self._clock.stop()
            if self._tz_before is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = self._tz_before
            time.tzset()

    def _install_langfuse(self, mp) -> None:
        import app.agent.core as core
        import app.agent.prompts as prompts_mod
        import app.agent.trace as trace

        mp.setattr(prompts_mod, "_get_client", lambda: self.langfuse)
        mp.setattr(trace, "_get_client", _no_langfuse)
        mp.setattr(core, "_get_trace_client", _no_langfuse)

    def _install_dynamic_config(self, mp) -> None:
        from inner_shared.dynamic_config import dynamic_config

        mp.setattr(
            dynamic_config,
            "_get_snapshot",
            lambda lane: {k: {"value": v} for k, v in self.config.items()},
        )

    async def _install_redis(self, mp) -> None:
        import fakeredis
        import fakeredis.aioredis

        import app.capabilities.redis as redis_cap
        import app.infra.redis as redis_infra

        # A server of its own. Without one, FakeRedis looks its server up by a random host name
        # (``uuid4().hex``), which the replay makes deterministic: every replay would share one
        # server, and a key one scenario set would still be there in the next.
        self.redis = fakeredis.aioredis.FakeRedis(
            server=fakeredis.FakeServer(), decode_responses=True
        )
        mp.setattr(redis_infra, "_redis", self.redis)
        mp.setattr(redis_cap, "_singleton", None)

    # ------------------------------------------------------------------ clock

    def at(self, moment: datetime) -> None:
        """Move the frozen clock. It does not move on its own."""
        self._frozen.move_to(moment)

    # ------------------------------------------------------------------ process

    async def start(self, app_name: str) -> None:
        """Start ``app_name``'s process; recorded as a step."""
        self.process = self._new_process(app_name)
        await self.step(f"start {app_name}", self.process.start)

    async def restart(self) -> None:
        """A new process of the same app; the old one's in-process state is cleared without
        recording (a killed process does not shut down, and what a graceful shutdown does is
        not part of any round). The start is recorded as a step."""
        assert self.process is not None, "replay: no process to restart"
        self.effects.revive()
        await self.process.stop()
        self.process = self._new_process(self.process.app_name)
        await self.step(f"restart {self.process.app_name}", self.process.start)

    def _new_process(self, app_name: str) -> AppProcess:
        self._started += 1
        return AppProcess(
            app_name,
            self._monkeypatch,
            self.broker,
            worker=f"{app_name}#{self._started}",
        )

    async def wake(self, participant: str, source: str) -> Any:
        """One tick of the wake ``source`` for ``participant``, the way the host's clock gives it:
        a signal at the frozen clock's time to the queue and source the running process registered
        (``ctx.wake``). Returns what the turn's ``run`` returned, ``None`` when no round was due."""
        from app.host import Signal

        assert self.process is not None, "replay: no process is running"
        (wake,) = [
            r
            for r in self.process.registered()
            if r.kind == "wake"
            and r.detail["turns"].participant == participant
            and r.detail["source"].name == source
        ]
        return await wake.detail["turns"].take(wake.detail["source"], Signal(at=now_cst()))

    async def tick(self, source: str) -> None:
        """One tick of the clock of the wake ``source``, the way the host's clock gives it: a
        signal at the frozen clock's time to every queue the running process registered for it
        (``ctx.wake``), all at once; an error in one queue's turn does not stop the others and is
        not raised (the host logs it). Returns nothing, like a clock's tick."""
        import asyncio

        from app.host import Signal

        assert self.process is not None, "replay: no process is running"
        signal = Signal(at=now_cst())
        wakes = [
            r
            for r in self.process.registered()
            if r.kind == "wake" and r.detail["source"].name == source
        ]
        assert wakes, f"replay: the process registered no wake {source!r}"
        await asyncio.gather(
            *(w.detail["turns"].take(w.detail["source"], signal) for w in wakes),
            return_exceptions=True,
        )

    def guide(self, name: str, text: str) -> None:
        """A guide on hand: ``SKILLS_DIR/<name>/SKILL.md``. The skills plugin loads the directory
        when the process starts, so call it before :meth:`start`."""
        folder = self._skills / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------ faults

    def misconfigure_prompt(self, prompt_id: str, variable: str) -> None:
        """Langfuse serves ``prompt_id`` declaring ``variable`` too, which no plugin gives (a
        release that added a variable the code does not know): every round that fetches it fails
        on a configuration error before its first model call, until :meth:`fix_prompt`."""
        self.langfuse.misconfigure(prompt_id, variable)

    def fix_prompt(self, prompt_id: str) -> None:
        """Langfuse serves ``prompt_id`` as its fixture again."""
        self.langfuse.fix(prompt_id)

    def fail_commits(
        self, matches: Callable[[list[str]], bool], *, times: int = 1
    ) -> None:
        """Commit failure: see :meth:`tests.replay.harness.database.WriteLog.fail_commits`."""
        self._writes.fail_commits(matches, times=times)

    def kill_after(self, matches: Callable[[dict[str, Any]], bool]) -> None:
        """The process dies right after the first effect ``matches`` accepts (see
        :mod:`tests.replay.harness.timeline`). Run that step with ``raises=ProcessKilled`` and
        :meth:`restart` before the next one."""
        self.effects.kill_after(matches)

    # ------------------------------------------------------------------ messaging

    def inbox(self, participant: str) -> str:
        """The queue name of ``participant``'s inbox on this lane."""
        from app.infra.rabbitmq import lane_queue
        from app.messaging.broker import inbox_route

        return lane_queue(inbox_route(participant).queue, self.lane)

    def scheduled(self) -> str:
        """The queue name of this lane's scheduled-delivery queue."""
        from app.infra.rabbitmq import lane_queue
        from app.messaging.broker import SCHEDULED

        return lane_queue(SCHEDULED.queue, self.lane)

    def message_arrives(
        self,
        *,
        sender: str,
        recipient: str,
        body: str,
        message_id: str,
        time: datetime | None = None,
        wakes_recipient: bool = True,
    ) -> str:
        """A message another process sent lands in ``recipient``'s inbox (not yet delivered to
        its consumer). Returns the inbox queue name, for :meth:`FakeBroker.deliver`."""
        from app.messaging.broker import headers
        from app.messaging.message import Kind, new_message

        message = new_message(
            sender=sender,
            recipient=recipient,
            body=body,
            kind=Kind.MESSAGE,
            time=time,
            message_id=message_id,
            wakes_recipient=wakes_recipient,
        )
        queue = self.inbox(recipient)
        self.broker.inject(queue, message.to_json(), headers())
        return queue

    # ------------------------------------------------------------------ steps

    async def step(
        self,
        name: str,
        action: Callable[[], Awaitable[Any]],
        *,
        at: datetime | None = None,
        raises: type[BaseException] | tuple[type[BaseException], ...] | None = None,
    ) -> Any:
        """Run ``action`` with every boundary recording; returns what it returned."""
        if at is not None:
            self.at(at)
        started = now_cst()
        rows_before, _ = await self._tables.read()
        files_before = read_tree(self.volume)
        self.model.calls.clear()
        self.effects.clear()
        self.broker.consumers_started.clear()
        self._writes.flush_open()
        self._writes.active = True
        outcome: dict[str, Any]
        result = None
        try:
            result = await action()
            outcome = {"returned": _describe(result)}
        except BaseException as exc:
            if raises is None or not isinstance(exc, raises):
                raise
            outcome = {"raised": describe_error(exc)}
        finally:
            await self.broker.settle()
            self._writes.flush_open()
            self._writes.active = False
            # A killed process does nothing more; the scenario restarts it before going on.
            self.effects.revive()
        rows_after, keys = await self._tables.read()
        record: dict[str, Any] = {
            "step": name,
            "at": started.isoformat(),
            "outcome": outcome,
            "model_calls": list(self.model.calls),
            "effects": list(self.effects),
            "consumers_started": sorted(self.broker.consumers_started),
            "rows": row_changes(rows_before, rows_after, keys),
            "files": file_changes(files_before, read_tree(self.volume)),
        }
        self.steps.append(record)
        if raises is not None and "raised" not in outcome:
            raise AssertionError(
                f"replay: step {name!r} was expected to raise {raises}"
            )
        return result

    # ------------------------------------------------------------------ baseline

    def check(self, name: str) -> None:
        """Compare everything recorded with ``baselines/<name>.json`` (or record it).

        A model call that found its script empty, and a request the replay refused (no answer
        given, or a host nothing serves), fail the replay here, before anything is compared or
        recorded, even when the step passed because the code under test swallowed the
        ``ScriptExhausted`` or ``UnservedRequest``."""
        refused = self.network.refused
        if refused:
            raise AssertionError(
                "replay: requests were refused: "
                + "; ".join(refused)
                + ". Give the scenario an answer for each (or serve the host in harness/). The "
                "code under test may have swallowed the UnservedRequest, so the steps passing "
                "proves nothing. Nothing was compared or recorded."
            )
        unanswered = self.network.unused()
        assert not unanswered, f"replay: answers never asked for: {unanswered}"
        ran_out = self.model.exhausted
        if ran_out:
            calls = ", ".join(f"{agent!r} call #{number}" for agent, number in ran_out)
            raise AssertionError(
                f"replay: the model script ran out: {calls} found no scripted reply; add one "
                f"for each. The code under test may have swallowed the ScriptExhausted, so the "
                f"steps passing proves nothing. Nothing was compared or recorded."
            )
        unused = self.model.unused()
        assert not unused, f"replay: scripted replies never used: {unused}"
        baseline.compare_or_record(
            name, baseline.build(name, self.steps, self.ids.produced)
        )
