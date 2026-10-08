"""Random ids made reproducible, and recognisable so the baseline can name them.

Every ``uuid.uuid4()`` call in the process returns a value derived from *where* it was called
(the caller's module and function) and *how many times* that place has asked so far. Two
consequences:

* A replay produces the same ids on every run, so anything derived from them (hashes, file
  names, sort orders) is stable.
* Adding a ``uuid4()`` call in one function does not shift the ids another function gets.

The ids never appear verbatim in a baseline: the document normaliser
(:mod:`tests.replay.harness.baseline`) replaces each one in :attr:`DeterministicIds.produced`
with ``<uuid:N>``, numbered by first appearance in the document. Ids
*derived* from a round's identity (``uuid5`` and friends) are not produced here and stay verbatim:
they are part of what the baseline compares.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from collections import Counter


class DeterministicIds:
    def __init__(self) -> None:
        self._calls: Counter[str] = Counter()
        self.produced: list[uuid.UUID] = []

    def uuid4(self) -> uuid.UUID:
        frame = sys._getframe(1)
        site = f"{frame.f_globals.get('__name__', '?')}:{frame.f_code.co_qualname}"
        self._calls[site] += 1
        digest = hashlib.sha256(f"{site}#{self._calls[site]}".encode()).digest()
        value = uuid.UUID(bytes=digest[:16], version=4)
        self.produced.append(value)
        return value

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(uuid, "uuid4", self.uuid4)
