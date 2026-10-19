"""The contract types a round is built from, which the host and the plugins hand to the agent layer.

They live here, under the agent layer, because the agent layer uses them and must not import the
host (``scripts/check_package_cycles.py`` forbids ``agent -> host``): the host imports them from
here and does the assembling.

So far there is one, :class:`Due`; the rest of a round's contract (the agent specs, profiles,
tool and segment declarations) joins it when the fixed round loop is built.
"""

from __future__ import annotations

from typing import Protocol


class Due(Protocol):
    """A round a wake source's ``due`` decided should run (:class:`app.host.turns.WakeSource`).

    Each source implements it with a dataclass of its own: the round's identity, the time it runs
    at and what it carries along are that source's fields (hers: the round id, the time she got
    her turn, the message that woke her; world's: the messages the round takes). The queue reads
    only ``reason``.
    """

    # Why this round runs: written to the log line the queue writes when it runs a round.
    reason: str
