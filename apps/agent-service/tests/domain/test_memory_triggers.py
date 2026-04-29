from app.domain.memory_triggers import DriftTrigger, AfterthoughtTrigger


def test_drift_trigger_is_transient():
    assert getattr(DriftTrigger.Meta, "transient", False) is True


def test_afterthought_trigger_is_transient():
    assert getattr(AfterthoughtTrigger.Meta, "transient", False) is True


def test_drift_trigger_dump_load():
    t = DriftTrigger(chat_id="c1", persona_id="p1")
    payload = t.model_dump(mode="json")
    assert payload == {"chat_id": "c1", "persona_id": "p1"}
    t2 = DriftTrigger(**payload)
    assert t2 == t


def test_afterthought_trigger_dump_load():
    t = AfterthoughtTrigger(chat_id="c1", persona_id="p1")
    payload = t.model_dump(mode="json")
    assert payload == {"chat_id": "c1", "persona_id": "p1"}
