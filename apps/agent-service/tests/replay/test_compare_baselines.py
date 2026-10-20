"""The baseline comparison tool (``tests/replay/tools/compare_baselines.py``): two directories of
baselines may differ only at the JSON paths it is told to allow; anything else, or a document in
only one of them, fails the comparison."""

from __future__ import annotations

import json
from pathlib import Path

from tests.replay.tools.compare_baselines import compare, main

CURR = "steps[*].model_calls[*].prompt.variables.currDate"


def _write(root: Path, name: str, document: dict) -> None:
    path = root / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _doc(variables: dict, *, said: str = "继续") -> dict:
    return {
        "baseline": "living_moment/x",
        "steps": [
            {
                "step": "a round",
                "model_calls": [
                    {
                        "agent": "living_life_moment",
                        "prompt": {
                            "name": "living_life_moment",
                            "variables": variables,
                        },
                        "reply": {"content": said},
                    }
                ],
            }
        ],
    }


def test_identical_baselines_compare_clean(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _write(
        old, "living_moment/x", _doc({"currDate": "2026-07-25", "persona_name": "赤尾"})
    )
    _write(
        new, "living_moment/x", _doc({"currDate": "2026-07-25", "persona_name": "赤尾"})
    )

    report = compare(old, new, allowed=[])

    assert report.disallowed == {} and report.only_in_one == []


def test_a_difference_under_an_allowed_path_is_allowed(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _write(
        old, "living_moment/x", _doc({"currDate": "2026-07-25", "persona_name": "赤尾"})
    )
    _write(new, "living_moment/x", _doc({"persona_name": "赤尾"}))

    report = compare(old, new, allowed=[CURR])

    assert report.disallowed == {}
    assert report.allowed == {
        "living_moment/x.json": ["steps[0].model_calls[0].prompt.variables.currDate"]
    }


def test_any_other_difference_is_reported_with_its_path(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _write(old, "living_moment/x", _doc({"currDate": "2026-07-25"}, said="继续"))
    _write(new, "living_moment/x", _doc({}, said="换了"))

    report = compare(old, new, allowed=[CURR])

    assert report.disallowed == {
        "living_moment/x.json": ["steps[0].model_calls[0].reply.content"]
    }


def test_a_document_in_only_one_directory_is_reported(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _write(old, "living_moment/x", _doc({}))
    _write(new, "living_moment/x", _doc({}))
    _write(new, "living_moment/y", _doc({}))

    report = compare(old, new, allowed=[])

    assert report.only_in_one == ["living_moment/y.json (only in new)"]


def test_the_command_fails_on_a_disallowed_difference(tmp_path, capsys):
    old, new = tmp_path / "old", tmp_path / "new"
    _write(old, "living_moment/x", _doc({"currDate": "2026-07-25"}))
    _write(new, "living_moment/x", _doc({"currDate": "2026-07-26"}))

    assert main([str(old), str(new)]) == 1
    assert (
        "steps[0].model_calls[0].prompt.variables.currDate" in capsys.readouterr().out
    )
    assert main([str(old), str(new), "--allow", CURR]) == 0
