"""Where a recorded replay is kept. A scenario's own file (``baselines/<kind>/<scenario>.json``)
holds what it did and, for each model call, the names of the tools it was offered; the tools'
schemas are kept once for every baseline, in ``baselines/tool_schemas.json``, keyed by tool name.

* Recording a scenario writes its own file and the schemas of the tools it offers; re-recording
  some scenarios keeps the schemas only the others offer, and drops the ones no baseline offers.
* Comparing fails when a tool the scenario offers has a different schema than the kept one, or
  none, exactly as a difference in the scenario's own file fails it.
* One scenario offering one tool name with two schemas fails: the shared file could keep only one.

Runs no scenario: documents are built from hand-made steps, into a temporary baselines directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.replay.harness import baseline


@pytest.fixture
def baselines(tmp_path, monkeypatch) -> Path:
    replay_dir = tmp_path / "replay"
    monkeypatch.setattr(baseline, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(baseline, "BASELINES", replay_dir / "baselines")
    monkeypatch.setattr(baseline, "ACTUAL", replay_dir / ".actual")
    monkeypatch.delenv(baseline.RECORD_ENV, raising=False)
    (replay_dir / "baselines").mkdir(parents=True)
    return replay_dir / "baselines"


def _tool(name: str, description: str = "做一件事。") -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": {}},
    }


def _call(*tools: dict, number: int = 1) -> dict:
    return {
        "agent": "living_life_moment",
        "number": number,
        "call": "complete",
        "model": "offline-model",
        "prompt": {"name": "living_life_moment", "version": 1, "variables": {}},
        "options": {},
        "tool_defs": list(tools),
        "messages": [{"role": "user", "content": f"第 {number} 次"}],
        "reply": {"role": "assistant", "content": "好。"},
    }


def _document(name: str, *calls: dict) -> dict:
    step = {"step": "a round", "at": "14:00", "outcome": {"returned": None}}
    return baseline.build(name, [{**step, "model_calls": list(calls)}], [])


def _record(monkeypatch, name: str, *tools: dict) -> None:
    monkeypatch.setenv(baseline.RECORD_ENV, name)
    baseline.compare_or_record(name, _document(name, _call(*tools)))
    monkeypatch.delenv(baseline.RECORD_ENV)


def _compare(name: str, *tools: dict) -> None:
    baseline.compare_or_record(name, _document(name, _call(*tools)))


def _kept(baselines: Path) -> dict:
    return json.loads((baselines / "tool_schemas.json").read_text(encoding="utf-8"))


def _schema(tool: dict) -> dict:
    return {"description": tool["description"], "parameters": tool["parameters"]}


def test_a_baseline_names_its_tools_and_their_schemas_are_kept_once(
    baselines, monkeypatch
):
    _record(monkeypatch, "kind/one", _tool("look_around"), _tool("say"))
    _record(monkeypatch, "kind/two", _tool("say"), _tool("stop_for_now"))

    one = json.loads((baselines / "kind/one.json").read_text(encoding="utf-8"))
    assert "tool_schemas" not in one
    assert one["steps"][0]["model_calls"][0]["tools"] == ["look_around", "say"]
    kept = (baselines / "tool_schemas.json").read_text(encoding="utf-8")
    assert (
        kept
        == json.dumps(
            {
                "look_around": _schema(_tool("look_around")),
                "say": _schema(_tool("say")),
                "stop_for_now": _schema(_tool("stop_for_now")),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    _compare("kind/one", _tool("look_around"), _tool("say"))
    _compare("kind/two", _tool("say"), _tool("stop_for_now"))


def test_re_recording_one_baseline_keeps_the_schemas_only_others_offer(
    baselines, monkeypatch
):
    _record(monkeypatch, "kind/one", _tool("look_around"), _tool("say"))
    _record(monkeypatch, "kind/two", _tool("say"), _tool("stop_for_now"))

    reworded = _tool("say", "说一句话。")
    _record(monkeypatch, "kind/one", _tool("look_around"), reworded)

    assert _kept(baselines) == {
        "look_around": _schema(_tool("look_around")),
        "say": _schema(reworded),
        "stop_for_now": _schema(_tool("stop_for_now")),
    }


def test_recording_drops_a_schema_no_baseline_offers_any_more(baselines, monkeypatch):
    _record(monkeypatch, "kind/one", _tool("look_around"), _tool("say"))
    _record(monkeypatch, "kind/two", _tool("say"))

    _record(monkeypatch, "kind/one", _tool("say"))

    assert _kept(baselines) == {"say": _schema(_tool("say"))}


def test_a_tool_described_differently_than_kept_fails_the_comparison(
    baselines, monkeypatch
):
    _record(monkeypatch, "kind/one", _tool("look_around"), _tool("say"))

    with pytest.raises(
        pytest.fail.Exception, match=r"differs at:\n  tool_schemas\.say\.description\n"
    ):
        _compare("kind/one", _tool("look_around"), _tool("say", "说一句话。"))


def test_a_hand_edit_to_the_kept_schemas_fails_the_comparison(baselines, monkeypatch):
    _record(monkeypatch, "kind/one", _tool("look_around"), _tool("say"))
    kept = _kept(baselines)
    kept["say"]["parameters"]["properties"] = {"what": {"type": "string"}}
    (baselines / "tool_schemas.json").write_text(json.dumps(kept), encoding="utf-8")

    with pytest.raises(
        pytest.fail.Exception,
        match=r"differs at:\n  tool_schemas\.say\.parameters\.properties\.what\n",
    ):
        _compare("kind/one", _tool("look_around"), _tool("say"))


def test_a_tool_with_no_kept_schema_fails_the_comparison(baselines, monkeypatch):
    _record(monkeypatch, "kind/one", _tool("look_around"), _tool("say"))
    kept = _kept(baselines)
    del kept["say"]
    (baselines / "tool_schemas.json").write_text(json.dumps(kept), encoding="utf-8")

    with pytest.raises(
        pytest.fail.Exception, match=r"differs at:\n  tool_schemas\.say\n"
    ):
        _compare("kind/one", _tool("look_around"), _tool("say"))


def test_one_tool_name_offered_with_two_schemas_fails(baselines):
    with pytest.raises(
        AssertionError, match=r"'say' was offered with two different schemas"
    ):
        _document(
            "kind/one",
            _call(_tool("say")),
            _call(_tool("say", "说一句话。"), number=2),
        )
