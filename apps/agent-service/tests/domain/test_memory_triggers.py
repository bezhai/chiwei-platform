from app.domain.memory_triggers import AfterthoughtTrigger


def test_afterthought_trigger_is_transient():
    assert getattr(AfterthoughtTrigger.Meta, "transient", False) is True


def test_afterthought_trigger_dump_load():
    t = AfterthoughtTrigger(chat_id="c1", persona_id="p1")
    payload = t.model_dump(mode="json")
    assert payload == {"chat_id": "c1", "persona_id": "p1"}


def test_drift_trigger_gone():
    """DriftTrigger 是 voice 再生成的触发信号，随 voice 子系统拆除一并删除。"""
    import app.domain.memory_triggers as mt

    assert not hasattr(mt, "DriftTrigger")
