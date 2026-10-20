"""The manifest checked against what the plugins registered, and the profiles filled in from both.

Plugins register what a round is made of under names (``ctx.tool``, ``ctx.segment``,
``ctx.section``, the steps, ``ctx.round_log``); the manifest's profiles and agents place those names
(:class:`app.agent.contracts.Profile`, :class:`app.agent.contracts.AgentSpec`). Once every plugin
has registered, before any IO, :func:`assemble` checks the two against each other and refuses, each
kind in one message listing every instance:

* a name registered twice, or listed twice in one list of a profile (``DuplicateContribution``);
* a name a profile or an agent lists that no plugin registered, an agent a profile names that the
  manifest does not list, a profile a participant names that it does not list
  (``UnknownContribution``);
* a registration no profile or agent lists (``UnplacedContribution``): something new has to be
  given its place in the manifest, which is what decides the order a round uses it in.

Then it fills in each profile a plugin asked for (:class:`app.agent.round.AssembledProfile`) with
what is registered under its names, in the profile's order.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.agent.contracts import Profile
from app.agent.round import AssembledProfile, Placed
from app.host.errors import (
    DuplicateContribution,
    UnknownContribution,
    UnplacedContribution,
)
from app.host.manifest import App

# The kinds of registration a manifest places.
KINDS = (
    "tool",
    "segment",
    "section",
    "round_start",
    "check",
    "commit",
    "after_commit",
    "round_log",
)


@dataclass(frozen=True)
class Contribution:
    """One registration of a kind a manifest places: under which name, by which plugin, what."""

    kind: str
    name: str
    plugin: str
    value: object


def _profile_lists(profile: Profile) -> list[tuple[str, str, tuple[str, ...]]]:
    """Each list of names in ``profile``: what it is called, the kind its names are, the names."""
    return [
        ("tools", "tool", profile.tools),
        ("state", "section", profile.state),
        ("input", "section", profile.input),
        ("round_start", "round_start", profile.round_start),
        ("checks", "check", profile.checks),
        ("commit", "commit", profile.commit),
        ("after_commit", "after_commit", profile.after_commit),
        ("round_log", "round_log", (profile.round_log,) if profile.round_log else ()),
    ]


def assemble(
    app_name: str,
    manifest: App,
    contributions: Iterable[Contribution],
    profiles: Mapping[str, AssembledProfile],
    *,
    lane: str,
) -> None:
    """Check ``manifest`` against ``contributions`` (see the module docstring), then fill in
    ``profiles`` (by profile name) for ``lane``."""
    registered: dict[tuple[str, str], list[Contribution]] = defaultdict(list)
    for c in contributions:
        registered[(c.kind, c.name)].append(c)

    duplicates = [
        f"{kind} {name!r} is registered by {', '.join(repr(c.plugin) for c in by)}"
        for (kind, name), by in registered.items()
        if len(by) > 1
    ]
    for profile_name, profile in manifest.profiles.items():
        for listing, _kind, names in _profile_lists(profile):
            twice = sorted({n for n in names if names.count(n) > 1})
            duplicates += [
                f"profile {profile_name!r} lists {name!r} twice in {listing}"
                for name in twice
            ]
    _refuse(app_name, DuplicateContribution, duplicates)

    placed: set[tuple[str, str]] = set()
    unknown: list[str] = []

    def need(kind: str, name: str, where: str) -> None:
        placed.add((kind, name))
        if (kind, name) not in registered:
            unknown.append(f"{where} lists {kind} {name!r}, which no plugin registered")

    for agent_name, spec in manifest.agents.items():
        for segment in spec.segments:
            need("segment", segment, f"agent {agent_name!r}")
        for tool in spec.tools:
            need("tool", tool, f"agent {agent_name!r}")
    for profile_name, profile in manifest.profiles.items():
        for agent in (profile.agent, *profile.sub_agents):
            if agent not in manifest.agents:
                unknown.append(
                    f"profile {profile_name!r} names agent {agent!r}, "
                    f"which the manifest does not list"
                )
        for _listing, kind, names in _profile_lists(profile):
            for name in names:
                need(kind, name, f"profile {profile_name!r}")
    unknown += [
        f"participant {p.name!r} has profile {p.profile!r}, which the manifest does not list"
        for p in manifest.participants
        if p.profile not in manifest.profiles
    ]
    _refuse(app_name, UnknownContribution, unknown)

    _refuse(
        app_name,
        UnplacedContribution,
        [
            f"{kind} {name!r} of plugin {by[0].plugin!r} is listed by no profile or agent"
            for (kind, name), by in registered.items()
            if (kind, name) not in placed
        ],
    )

    one = {key: by[0] for key, by in registered.items()}

    def at(kind: str, names: tuple[str, ...]) -> tuple[Placed, ...]:
        return tuple(
            Placed(n, one[(kind, n)].plugin, one[(kind, n)].value) for n in names
        )

    for profile_name, assembled in profiles.items():
        profile = manifest.profiles[profile_name]
        agents = {n: manifest.agents[n] for n in (profile.agent, *profile.sub_agents)}
        assembled.lane = lane
        assembled.agents = agents
        assembled.segments = {
            s: one[("segment", s)].value
            for spec in agents.values()
            for s in spec.segments
        }
        assembled.tools = at("tool", profile.tools)
        assembled.round_start = at("round_start", profile.round_start)
        assembled.state = at("section", profile.state)
        assembled.input = at("section", profile.input)
        assembled.checks = at("check", profile.checks)
        assembled.commit = at("commit", profile.commit)
        assembled.after_commit = at("after_commit", profile.after_commit)
        assembled.round_log = (
            one[("round_log", profile.round_log)].value if profile.round_log else None
        )
        assembled.assembled = True


def _refuse(app_name: str, error: type[Exception], problems: list[str]) -> None:
    if problems:
        raise error(f"app {app_name!r}: " + "; ".join(problems))
