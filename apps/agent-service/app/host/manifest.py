"""An app's manifest, as the host reads it: which plugins it loads, its agents, the profiles its
participants' rounds are made of, and its participants.

Only data: strings and the contract types of :mod:`app.agent.contracts`. A manifest names its
plugins by module path and imports none of them, so an app's process loads only the code its
manifest names. The host checks a manifest against what its plugins registered and does the
assembling; the agent layer never sees this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.agent.contracts import AgentSpec, Profile


@dataclass(frozen=True)
class Participant:
    """Someone the app runs rounds for, and the profile their rounds are made of."""

    name: str
    profile: str


@dataclass(frozen=True)
class App:
    """One app: its plugins (module paths, imported when the host is built), its agents by name
    (the original prompt id), its profiles by name, and its participants."""

    plugins: tuple[str, ...]
    agents: Mapping[str, AgentSpec]
    profiles: Mapping[str, Profile]
    participants: tuple[Participant, ...]
