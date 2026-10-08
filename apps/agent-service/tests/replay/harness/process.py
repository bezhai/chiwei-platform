"""One app process inside the test: started the way ``app.main``'s lifespan starts it, minus the
parts the scenario drives itself.

Start: fresh in-process state (registries, messaging's module state, residents, the volume
lock), that app's wiring executed again (``app.deployment.APP_WIRING``), the graph compiled
(``prepare_for_run``), then durable consumers, messaging (inboxes, question queues, scheduled
delivery) and debounce consumers. Not started: the interval clocks, the HTTP routes, the
outbox dispatcher, the skill reload loop. Rounds run when the scenario calls them, and the
skill registry is empty (no guides on hand), as it is wherever ``SKILLS_DIR`` has none.

Stop mirrors the lifespan's shutdown. A restart is stop + start with the database, the broker's
queues and the volume left as they were, which is what a new process finds.

The durable consumer names itself ``hostname:pid`` (``app.runtime.durable.WORKER_ID``) on the
inflight rows it claims; a replayed process is named ``<app>#<n>`` (the n-th process the scenario
started) instead, so a claim a dead process left behind reads the same on every machine.
"""

from __future__ import annotations

import asyncio
import importlib
from inspect import ismodule


def _fresh_process_state(monkeypatch) -> None:
    """Everything a new process would start without. Module-level state of the messaging
    layer is reset here because a test process has run other suites before this one."""
    from inner_shared.dynamic_config import dynamic_config

    import app.living.participants as participants
    import app.messaging.receiving as receiving
    import app.messaging.sending as sending
    import app.runtime.debounce as debounce
    import app.runtime.durable as durable
    import app.world.volume as volume
    from app.messaging.receiving import clear_inboxes
    from app.runtime.emit import reset_emit_runtime
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring
    from app.skills.registry import SkillRegistry
    from app.world.sources import clear_sources

    clear_wiring()
    clear_bindings()
    clear_inboxes()
    clear_sources()
    reset_emit_runtime()
    for module, name, value in (
        (receiving, "_consumers", []),
        (receiving, "_in_flight", set()),
        (receiving, "_answering", set()),
        (receiving, "_held_openers", []),
        (receiving, "_let_go", None),
        (receiving, "_stopping", False),
        (receiving, "_put_backs", {}),
        (receiving, "_putting_back", set()),
        (sending, "_reply_rk", None),
        (sending, "_reply_channel", None),
        (sending, "_reply_lock", None),
        (sending, "_waiting", {}),
        (durable, "_consumer_tags", []),
        (debounce, "_consumer_tags", []),
        (participants, "_known", None),
        (volume, "_held", None),
    ):
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(SkillRegistry, "_skills", {})
    monkeypatch.setattr(dynamic_config, "_lane_provider", dynamic_config._lane_provider)


def _execute_wiring(app_name: str) -> None:
    """Run the app's wiring modules' bodies again (their ``wire`` / ``inbox`` / ``register``
    calls), submodules of a wiring package first, in the order the package imports them."""
    from app.deployment import APP_WIRING

    for module_name in APP_WIRING[app_name]:
        module = importlib.import_module(module_name)
        for sub in [
            v
            for v in vars(module).values()
            if ismodule(v) and v.__name__.startswith(f"{module_name}.")
        ]:
            importlib.reload(sub)
        importlib.reload(module)


class AppProcess:
    def __init__(
        self, app_name: str, monkeypatch, broker, *, worker: str | None = None
    ) -> None:
        self.app_name = app_name
        self._monkeypatch = monkeypatch
        self._broker = broker
        self._worker = worker
        self.running = False

    async def start(self) -> None:
        from app.infra.rabbitmq import lane_queue
        from app.messaging.broker import inbox_route, lane
        from app.messaging.lifecycle import start_messaging
        from app.messaging.receiving import INBOX_REGISTRY
        from app.runtime.bootstrap import prepare_for_run
        from app.runtime.debounce import start_debounce_consumers
        from app.runtime.durable import start_consumers

        self._monkeypatch.setenv("APP_NAME", self.app_name)
        if self._worker is not None:
            import app.runtime.durable as durable

            self._monkeypatch.setattr(durable, "WORKER_ID", self._worker)
        _fresh_process_state(self._monkeypatch)
        _execute_wiring(self.app_name)
        await prepare_for_run(self.app_name)
        await start_consumers(app_name=self.app_name)
        held = [s.name for s in INBOX_REGISTRY.values() if s.consume_while is not None]
        await start_messaging()
        await start_debounce_consumers(app_name=self.app_name)
        self.running = True
        # Inboxes that consume only while holding something (world's volume lock) open in the
        # background; the process is up once they consume.
        for name in held:
            queue = lane_queue(inbox_route(name).queue, lane())
            async with asyncio.timeout(10):
                while (
                    not self._broker.queues.get(queue)
                    or not self._broker.queues[queue].consumers
                ):
                    await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if not self.running:
            return
        from app.messaging.lifecycle import stop_messaging
        from app.runtime.debounce import stop_debounce_consumers
        from app.runtime.durable import stop_consumers

        self.running = False
        await stop_debounce_consumers()
        await stop_messaging()
        await stop_consumers()
