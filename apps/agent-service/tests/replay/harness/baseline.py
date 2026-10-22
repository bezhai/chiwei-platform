"""The baseline document: building it from the recorded steps, normalising it, and comparing it
with (or recording it as) ``tests/replay/baselines/<name>.json`` and the shared
``tests/replay/baselines/tool_schemas.json``.

Layout of a document as built::

    {
      "baseline": "<kind>/<scenario>",
      "steps": [ {step}, ... ],          # in the order the scenario ran them
      "tool_schemas": {name: {description, parameters}}   # every tool any call offered
    }

It is kept in two places. ``baselines/<name>.json`` holds everything but ``tool_schemas``; the
schemas are kept once for all baselines, in ``baselines/tool_schemas.json`` (``{name:
{description, parameters}}``, sorted by name, normalised like the rest). They are keyed by tool
name because a tool's schema comes from its definition alone: every agent and every scenario that
offers it is offered the same one (``test_tool_schemas.py`` checks this across agents). One
scenario offering one name with two schemas fails ``build``, since the shared file could keep only
one of them.

Comparing a scenario compares its own file byte for byte and, for each tool it offered, the kept
schema; a difference in either fails it the same way. Recording a scenario writes its own file, sets
the kept schema of each tool it offered, and drops the schemas no baseline names in a model call's
``tools`` any more; the schemas only other baselines offer stay as they are.

A step::

    {
      "step": "...", "at": "<clock when it started>",
      "outcome": {"returned": ...} | {"raised": "Type: message"},
      "model_calls": [...],        # see below
      "effects": [...],            # in the order they happened: write transactions ending
                                   # ({"db": "commit", "writes": [...]}), publishes, deliveries
                                   # and how they settled, file writes and deletes
      "consumers_started": [...],  # queues that got a consumer during the step
      "rows": {table: {"added"|"changed"|"removed": [...]}},
      "files": {path: {"added"|"before"/"after"|"removed": ...}}
    }

A model call lists the tools it offered by name (their schemas are in ``tool_schemas``). Its
messages are written out in full the first time; a later call of the same agent in the same
step whose messages begin with an earlier call's messages says so (``"continues"``) and lists
only what was added (``"then"``) — the ReAct loop resends the whole conversation every turn.

Normalisation: ids produced by ``uuid4`` during the scenario become ``<uuid:N>`` (by first
appearance; a truncated one ``<uuid:N>[:k]``); the payload of a base64 ``data:`` URI becomes
``<N bytes, sha256:12 hex>`` (the mime type stays); the SQLAlchemy release in its error help
links (``https://sqlalche.me/e/20/...``, which the code stores in full in some error columns)
becomes ``<release>``; any string spanning several lines becomes a list of its lines, so a diff
points at the line that changed. Nothing else is rewritten.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

REPLAY_DIR = Path(__file__).resolve().parent.parent
BASELINES = REPLAY_DIR / "baselines"
ACTUAL = REPLAY_DIR / ".actual"

# The schemas of the tools the baselines offer, kept once for all of them (under ``BASELINES``).
TOOL_SCHEMAS = "tool_schemas.json"

# ``REPLAY_RECORD=1`` (or ``all``) re-records every baseline the run reaches; otherwise a
# comma-separated list of names or globs (``living_moment/*``) picks which.
RECORD_ENV = "REPLAY_RECORD"

_DIFF_LINES = 160


def _call_label(call: dict[str, Any]) -> str:
    return f"{call['agent']} #{call['number']}"


def _tool_names(defs: list[dict[str, Any]], catalogue: dict[str, Any]) -> list[str]:
    names = []
    for d in defs:
        schema = {"description": d["description"], "parameters": d["parameters"]}
        if catalogue.setdefault(d["name"], schema) != schema:
            raise AssertionError(
                f"replay: tool {d['name']!r} was offered with two different schemas in one "
                f"scenario; baselines/{TOOL_SCHEMAS} keeps one schema per tool name, so it "
                f"cannot hold both. Key the kept schemas by something that keeps them apart "
                f"(harness/baseline.py)."
            )
        names.append(d["name"])
    return names


def _calls(
    raw: list[dict[str, Any]], catalogue: dict[str, Any]
) -> list[dict[str, Any]]:
    out = []
    for i, call in enumerate(raw):
        entry: dict[str, Any] = {
            "agent": call["agent"],
            "number": call["number"],
            "call": call["call"],
            "model": call["model"],
            "prompt": call["prompt"],
            "options": dict(sorted(call["options"].items())),
            "tools": _tool_names(call["tool_defs"], catalogue),
        }
        if "schema" in call:
            entry["schema"] = call["schema"]
        messages = call["messages"]
        earlier = next(
            (
                prev
                for prev in reversed(raw[:i])
                if prev["agent"] == call["agent"]
                and len(prev["messages"]) <= len(messages)
                and messages[: len(prev["messages"])] == prev["messages"]
            ),
            None,
        )
        if earlier is None:
            entry["messages"] = messages
        else:
            entry["continues"] = _call_label(earlier)
            entry["then"] = messages[len(earlier["messages"]) :]
        if "raised" in call:
            entry["raised"] = call["raised"]
        else:
            entry["reply"] = call.get("reply")
        out.append(entry)
    return out


def build(name: str, steps: list[dict[str, Any]], produced_ids: list) -> dict[str, Any]:
    catalogue: dict[str, Any] = {}
    shaped = []
    for step in steps:
        shaped.append({**step, "model_calls": _calls(step["model_calls"], catalogue)})
    document = {
        "baseline": name,
        "steps": shaped,
        "tool_schemas": dict(sorted(catalogue.items())),
    }
    return _Normaliser(produced_ids).apply(document)


# A base64 ``data:`` URI (an inline image a model request or a stored row may carry).
_DATA_URI = re.compile(
    r"(data:[\w.+-]*/?[\w.+-]*(?:;[\w.+-]+=[^;,]*)*;base64,)([A-Za-z0-9+/=]+)"
)


def _describe_data_uri(match: re.Match) -> str:
    """The payload of a ``data:`` URI as its length and a hash: bytes do not belong in a
    baseline, and the length and hash still change when the content does."""
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        return match.group(0)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    return f"{match.group(1)}<{len(raw)} bytes, sha256:{digest}>"


# SQLAlchemy appends a help link naming its release series to every error message; code that
# stores ``str(exc)`` (an inflight row's ``last_error``) would tie a baseline to the release.
_SQLALCHEMY_HELP = re.compile(r"https://sqlalche\.me/e/\d+/")


class _Normaliser:
    _CANDIDATE = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{8,32}"
    )

    def __init__(self, produced_ids: list) -> None:
        self._by_form: dict[str, Any] = {}
        for value in produced_ids:
            self._by_form[str(value)] = value
            self._by_form[value.hex] = value
        self._hexes = [v.hex for v in produced_ids]
        self._names: dict[Any, str] = {}

    def _name(self, value) -> str:
        if value not in self._names:
            self._names[value] = f"<uuid:{len(self._names) + 1}>"
        return self._names[value]

    def _replace(self, match: re.Match) -> str:
        token = match.group(0)
        value = self._by_form.get(token)
        if value is not None:
            return self._name(value)
        if len(token) < 32:
            for full in self._hexes:
                if full.startswith(token):
                    return f"{self._name(self._by_form[full])}[:{len(token)}]"
        return token

    def _string(self, s: str) -> str:
        if "data:" in s:
            s = _DATA_URI.sub(_describe_data_uri, s)
        if "sqlalche.me" in s:
            s = _SQLALCHEMY_HELP.sub("https://sqlalche.me/e/<release>/", s)
        return self._CANDIDATE.sub(self._replace, s) if self._by_form else s

    def apply(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {self._string(str(k)): self.apply(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [self.apply(v) for v in node]
        if isinstance(node, str):
            s = self._string(node)
            return s.split("\n") if "\n" in s else s
        return node


def _render(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _recording(name: str) -> bool:
    wanted = os.getenv(RECORD_ENV, "").strip()
    if not wanted:
        return False
    if wanted.lower() in {"1", "all", "true", "yes"}:
        return True
    return any(fnmatch.fnmatch(name, p.strip()) for p in wanted.split(",") if p.strip())


def _differences(old: Any, new: Any, path: str = "") -> list[str]:
    """Where two documents differ, as JSON paths (``steps[1].rows.data_life_moment...``)."""
    if type(old) is not type(new):
        return [path or "(document)"]
    if isinstance(old, dict):
        found = []
        for key in list(old) + [k for k in new if k not in old]:
            where = f"{path}.{key}" if path else str(key)
            if key not in old or key not in new:
                found.append(where)
            else:
                found += _differences(old[key], new[key], where)
        return found
    if isinstance(old, list):
        found = []
        for i in range(min(len(old), len(new))):
            found += _differences(old[i], new[i], f"{path}[{i}]")
        if len(old) != len(new):
            found.append(f"{path} (length {len(old)} -> {len(new)})")
        return found
    return [] if old == new else [path]


def read_tool_schemas() -> dict[str, Any]:
    """The kept schemas, ``{name: {description, parameters}}`` (``{}`` while there are none)."""
    path = BASELINES / TOOL_SCHEMAS
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def offered_tools() -> set[str]:
    """The name of every tool a model call in some baseline was offered."""
    return {
        tool
        for path in BASELINES.glob("*/*.json")
        for step in json.loads(path.read_text(encoding="utf-8"))["steps"]
        for call in step["model_calls"]
        for tool in call.get("tools") or []
    }


@contextmanager
def _one_recorder_at_a_time() -> Iterator[None]:
    """Scenarios run in parallel (``-n auto``). A recording reads the kept schemas and every
    baseline and rewrites the schemas; holding a lock on the baselines directory meanwhile, it
    neither loses another recording's schemas nor reads a baseline another one is writing."""
    BASELINES.mkdir(parents=True, exist_ok=True)
    fd = os.open(BASELINES, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _record(name: str, own: dict[str, Any], offered: dict[str, Any]) -> None:
    path = BASELINES / f"{name}.json"
    actual = _render(own)
    with _one_recorder_at_a_time():
        if not path.exists() or path.read_text(encoding="utf-8") != actual:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(actual, encoding="utf-8")
            print(f"replay: recorded {path.relative_to(REPLAY_DIR.parent.parent)}")
        kept = read_tool_schemas()
        merged = {**kept, **offered}
        in_use = offered_tools()
        schemas = {tool: merged[tool] for tool in sorted(merged) if tool in in_use}
        target = BASELINES / TOOL_SCHEMAS
        rendered = _render(schemas)
        if target.exists() and target.read_text(encoding="utf-8") == rendered:
            return
        # A scenario comparing meanwhile (not under the lock) reads the old file or the new one,
        # never half of one.
        staged = target.with_name(f"{TOOL_SCHEMAS}.tmp")
        staged.write_text(rendered, encoding="utf-8")
        os.replace(staged, target)
        changed = [tool for tool in offered if kept.get(tool) != offered[tool]]
        dropped = [tool for tool in kept if tool not in schemas]
        print(
            f"replay: recorded {target.relative_to(REPLAY_DIR.parent.parent)} "
            f"(set: {', '.join(changed) or '-'}; dropped: {', '.join(dropped) or '-'})"
        )


def compare_or_record(name: str, document: dict[str, Any]) -> None:
    own = {key: value for key, value in document.items() if key != "tool_schemas"}
    offered = document["tool_schemas"]
    if _recording(name):
        _record(name, own, offered)
        return
    path = BASELINES / f"{name}.json"
    if not path.exists():
        pytest.fail(
            f"replay: no baseline {path.relative_to(REPLAY_DIR)}; record it with "
            f"{RECORD_ENV}={name} and review the file before committing it",
            pytrace=False,
        )
    expected = path.read_text(encoding="utf-8")
    actual = _render(own)
    all_kept = read_tool_schemas()
    kept = {tool: all_kept[tool] for tool in offered if tool in all_kept}
    if expected == actual and kept == offered:
        return
    where: list[str] = []
    diff: list[str] = []
    out = None
    if expected != actual:
        out = ACTUAL / f"{name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(actual, encoding="utf-8")
        where += _differences(json.loads(expected), json.loads(actual))
        diff += difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=f"baselines/{name}.json",
            tofile=f".actual/{name}.json",
            lineterm="",
        )
    if kept != offered:
        where += _differences({"tool_schemas": kept}, {"tool_schemas": offered})
        diff += difflib.unified_diff(
            _render(kept).splitlines(),
            _render(offered).splitlines(),
            fromfile=f"baselines/{TOOL_SCHEMAS} (the tools this scenario offers)",
            tofile="the tools as offered in this replay",
            lineterm="",
        )
    shown = ["differs at:", *(f"  {w}" for w in where[:20])]
    if len(where) > 20:
        shown.append(f"  ... and {len(where) - 20} more")
    shown += diff[:_DIFF_LINES]
    if len(diff) > _DIFF_LINES:
        shown.append(
            f"... {len(diff) - _DIFF_LINES} more diff lines"
            + (f"; see {out}" if out else "")
        )
    written = f"Full replay written to {out}. " if out else ""
    pytest.fail(
        "replay: behaviour differs from the baseline\n"
        + "\n".join(shown)
        + f"\n\n{written}If every difference is intended, list each one and re-record with "
        f"{RECORD_ENV}={name}.",
        pytrace=False,
    )
