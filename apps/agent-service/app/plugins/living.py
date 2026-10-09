"""Living: the three sisters' life engine — five clocks, the reading edge, the two outbound queues
and the sisters' inboxes.

  every  60 s  LifeMomentTick     -> life_moment_tick     her rounds (the node decides who is due)
  every  60 s  PhoneNudgeTick     -> phone_nudge_tick     wake her early for a call or a message
  every 300 s  LandingTick        -> landing_tick         match what she said with what landed
  every 300 s  DayPageTick        -> day_page_tick        yesterday's page (04:00-06:00 window)
  every 300 s  PersonaReviewTick  -> persona_review_tick  Monday's "who I am" (06:00-08:00 window)

  FilePickedUp        -> read_a_round                     durable: one stretch of reading
  ChatResponseSegment -> chat_response_{channel}          what she says
  Recall              -> recall_{channel}                 what she takes back

  the inboxes named after the three personas -> received  stored only; she reads them on her clock

**Clocks tick denser than the work they drive.** A clock's seconds are fixed when it is
registered, so the intervals that are business parameters (ten minutes between rounds, the
diary and review windows) are judged inside the nodes, on every tick; most ticks return before
reading anything.

**A tick builds its payload in the clock loop** (:func:`_ticker`): a tick Data that grew a
required field fails there, and the clock's watchdog stops the process, as the dataflow interval
source did. The node's run is the tick's work and runs on its own.

**She has no inbound edge.** Messages on her phone she reads from ``common_message`` in her own
rounds; what world and her sisters tell her arrives in her inbox, which only stores it. The
durable edge carries only the file she picked up in one of her own rounds
(``tests/living/test_no_inbound.py``).

**The inboxes are opened at start**: their names come from the persona table, which cannot be read
at import (:func:`app.living.received.open_inboxes`).

**``records`` and ``pictures`` are imported for their Data classes.** A Data class registers when
its module is imported, and the schema step builds tables only for registered classes. Both are
also reached through ``moment`` today; importing them here keeps their tables from depending on
that chain. The other living Data classes come in with the modules below.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from app.domain.chat_dataflow import ChatResponseSegment
from app.domain.safety import Recall
from app.host import Context, Plugin, Tick
from app.living import pictures, records  # noqa: F401
from app.living.day_page import DAY_PAGE_TICK_SECONDS, DayPageTick, day_page_tick
from app.living.landing import LANDING_TICK_SECONDS, LandingTick, landing_tick
from app.living.moment import LIFE_MOMENT_TICK_SECONDS, LifeMomentTick, life_moment_tick
from app.living.nudge import PHONE_NUDGE_TICK_SECONDS, PhoneNudgeTick, phone_nudge_tick
from app.living.persona_review import (
    PERSONA_REVIEW_TICK_SECONDS,
    PersonaReviewTick,
    persona_review_tick,
)
from app.living.reading import FilePickedUp, read_a_round
from app.living.received import open_inboxes
from app.runtime.data import Data

# (tick Data, seconds, node). The clock is named after its tick Data.
CLOCKS: tuple[tuple[type[Data], float, Callable[[Data], Awaitable[None]]], ...] = (
    (LifeMomentTick, LIFE_MOMENT_TICK_SECONDS, life_moment_tick),
    (PhoneNudgeTick, PHONE_NUDGE_TICK_SECONDS, phone_nudge_tick),
    (LandingTick, LANDING_TICK_SECONDS, landing_tick),
    (DayPageTick, DAY_PAGE_TICK_SECONDS, day_page_tick),
    (PersonaReviewTick, PERSONA_REVIEW_TICK_SECONDS, persona_review_tick),
)


def _ticker(data_type: type[Data], node: Callable[[Data], Awaitable[None]]) -> Tick:
    """Build ``data_type(ts=<iso>)`` now, in the clock loop; hand it to ``node`` as the work."""

    def tick(ts: datetime) -> Awaitable[None]:
        return node(data_type(ts=ts.isoformat()))

    return tick


def setup(ctx: Context) -> None:
    for data_type, seconds, node in CLOCKS:
        ctx.clock(data_type.__name__, seconds, _ticker(data_type, node))
    ctx.durable(FilePickedUp, read_a_round)
    ctx.outbound(ChatResponseSegment, "chat_response")
    ctx.outbound(Recall, "recall")
    ctx.inboxes_at_start(open_inboxes)


PLUGIN = Plugin(name="living", setup=setup, requires=("skills",))
