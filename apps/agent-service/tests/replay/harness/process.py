"""One app process inside the test: started the way ``app.main``'s lifespan starts it, minus the
parts the scenario drives itself.

Start: fresh in-process state (registries, messaging's module state, residents, the volume
lock), then the app's plugin host (``Host.for_app``, the manifest in ``app.deployment.APPS``)
with the broker phases only: every plugin's setup, the graph compiled, the durable routes
declared, durable consumers and messaging (inboxes, question queues, scheduled delivery). Not
started: the schema step (the replay builds the schema once, :func:`create_schema`), the interval
clocks, the HTTP routes, the skill reload task. Rounds run when the scenario calls them. The skills
plugin loads ``SKILLS_DIR``, which the replay points at an empty directory, so the skill registry is
empty (no guides on hand).

Stop is the host's stop: messaging, durable consumers, then every registration taken back. A
restart is stop + start with the database, the broker's queues and the volume left as they were,
which is what a new process finds.

The durable consumer names itself ``hostname:pid`` (``app.runtime.durable.WORKER_ID``) on the
inflight rows it claims; a replayed process is named ``<app>#<n>`` (the n-th process the scenario
started) instead, so a claim a dead process left behind reads the same on every machine.
"""

from __future__ import annotations

import asyncio


def _fresh_process_state(monkeypatch) -> None:
    """Everything a new process would start without. Module-level state of the messaging
    layer is reset here because a test process has run other suites before this one."""
    from inner_shared.dynamic_config import dynamic_config

    import app.living.participants as participants
    import app.messaging.receiving as receiving
    import app.messaging.sending as sending
    import app.runtime.durable as durable
    import app.world.volume as volume
    from app.messaging.receiving import clear_inboxes
    from app.runtime.emit import reset_emit_runtime
    from app.runtime.wire import clear_wiring
    from app.skills.registry import SkillRegistry
    from app.world.sources import clear_sources

    clear_wiring()
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
        (participants, "_known", None),
        (volume, "_held", None),
    ):
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(SkillRegistry, "_skills", {})
    monkeypatch.setattr(dynamic_config, "_lane_provider", dynamic_config._lane_provider)


class AppProcess:
    def __init__(
        self, app_name: str, monkeypatch, broker, *, worker: str | None = None
    ) -> None:
        self.app_name = app_name
        self._monkeypatch = monkeypatch
        self._broker = broker
        self._worker = worker
        self._host = None

    async def start(self) -> None:
        from app.host import Host
        from app.infra.rabbitmq import lane_queue
        from app.messaging.broker import inbox_route, lane

        self._monkeypatch.setenv("APP_NAME", self.app_name)
        if self._worker is not None:
            import app.runtime.durable as durable

            self._monkeypatch.setattr(durable, "WORKER_ID", self._worker)
        _fresh_process_state(self._monkeypatch)
        host = Host.for_app(self.app_name)
        await host.start(http=None, schema=False, mq=True, clocks=False, tasks=False)
        self._host = host
        # Inboxes that consume only while holding something (world's volume lock) open in the
        # background; the process is up once they consume.
        held = [
            r.name
            for r in host.registered()
            if r.kind == "inbox" and r.detail["consume_while"] is not None
        ]
        for name in held:
            queue = lane_queue(inbox_route(name).queue, lane())
            async with asyncio.timeout(10):
                while (
                    not self._broker.queues.get(queue)
                    or not self._broker.queues[queue].consumers
                ):
                    await asyncio.sleep(0.01)

    async def stop(self) -> None:
        host, self._host = self._host, None
        if host is not None:
            await host.stop()
