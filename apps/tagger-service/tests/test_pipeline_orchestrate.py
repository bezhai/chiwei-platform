from __future__ import annotations

import pytest

from app.pipeline.orchestrate import TaggerStage, run_pipeline


class FakeTagger:
    def __init__(self, name: str, result: dict) -> None:
        self.name = name
        self._result = result

    def tag(self, image: object) -> dict:
        return dict(self._result)


class BoomTagger:
    name = "boom"

    def tag(self, image: object) -> dict:
        raise RuntimeError("kaboom")


class FakeStage:
    """假阶段：记录 load/run/unload 调用顺序，run 返回预设的 {id: {name: result}}。"""

    def __init__(self, out: dict) -> None:
        self._out = out
        self.events: list[str] = []

    def load(self) -> None:
        self.events.append("load")

    def run(self, items: list[tuple[str, object]]) -> dict:
        self.events.append("run")
        return {k: {n: dict(r) for n, r in v.items()} for k, v in self._out.items()}

    def unload(self) -> None:
        self.events.append("unload")


def test_run_pipeline_merges_stages_per_id() -> None:
    # 两个阶段各产不同能力 → 按 id 合并成一行
    stage1 = FakeStage({"a": {"phash": {"phash": "x"}}, "b": {"phash": {"phash": "y"}}})
    stage2 = FakeStage({"a": {"ocr": {"ocr_text": "hi"}}, "b": {"ocr": {"ocr_text": ""}}})
    rows, dups = run_pipeline([("a", 1), ("b", 2)], [stage1, stage2])
    row_a = next(r for r in rows if r["id"] == "a")
    assert row_a["phash"]["phash"] == "x"
    assert row_a["ocr"]["ocr_text"] == "hi"
    assert dups == []


def test_run_pipeline_calls_load_run_unload_in_order() -> None:
    stage = FakeStage({"a": {"x": {}}})
    run_pipeline([("a", 1)], [stage])
    assert stage.events == ["load", "run", "unload"]


def test_run_pipeline_unloads_even_if_run_raises() -> None:
    # 阶段 run 抛异常时仍 unload（释放显存防泄漏），异常上抛
    class BoomStage(FakeStage):
        def run(self, items: list) -> dict:
            self.events.append("run")
            raise RuntimeError("stage boom")

    stage = BoomStage({})
    with pytest.raises(RuntimeError):
        run_pipeline([("a", 1)], [stage])
    assert stage.events == ["load", "run", "unload"]


def test_run_pipeline_dedups_ids() -> None:
    stage = FakeStage({"a": {"x": {}}})
    rows, dups = run_pipeline([("a", 1), ("a", 2)], [stage])
    assert len(rows) == 1
    assert dups == ["a"]


def test_tagger_stage_runs_each_tagger_per_image() -> None:
    stage = TaggerStage([
        lambda: FakeTagger("phash", {"phash": "x"}),
        lambda: FakeTagger("anime_rating", {"safe": 0.9}),
    ])
    stage.load()
    out = stage.run([("a", 1), ("b", 2)])
    stage.unload()
    assert out["a"]["phash"]["phash"] == "x"
    assert out["a"]["anime_rating"]["safe"] == 0.9
    assert out["b"]["phash"]["phash"] == "x"


def test_tagger_stage_isolates_tagger_exception() -> None:
    stage = TaggerStage([lambda: BoomTagger(), lambda: FakeTagger("phash", {"phash": "x"})])
    stage.load()
    out = stage.run([("a", 1)])
    assert "error" in out["a"]["boom"]
    assert out["a"]["phash"]["phash"] == "x"


def test_tagger_stage_constructs_lazily_on_load() -> None:
    # 工厂延迟构造：构造 stage 不触发打标器构造（onnx 占显存的等到本阶段 load 才 load，不抢 Qwen 显存）
    constructed: list[int] = []

    def factory() -> FakeTagger:
        constructed.append(1)
        return FakeTagger("x", {})

    stage = TaggerStage([factory])
    assert constructed == []
    stage.load()
    assert constructed == [1]
