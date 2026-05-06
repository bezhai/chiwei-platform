"""Phase 4 life_dataflow Data classes — surface check."""
from __future__ import annotations

import pytest

from app.runtime.data import key_fields


def test_all_classes_importable():
    from app.domain import life_dataflow as ld

    for name in [
        "MinuteTick", "LightDayTick", "LightNightTick", "HeavyReviewTick",
        "DailyPlanTick", "GlimpseTick",
        "LifeTickRequest", "VoiceRequest", "LightReviewRequest",
        "HeavyReviewRequest", "GlimpseTickRequest",
        "SharedDailyContext", "DailyPlanRequest",
        "LifeStateChanged", "GlimpseRequest",
    ]:
        assert hasattr(ld, name), f"{name} missing"


def test_tick_classes_are_transient():
    from app.domain.life_dataflow import (
        MinuteTick, LightDayTick, LightNightTick, HeavyReviewTick,
        DailyPlanTick, GlimpseTick,
    )
    for cls in [MinuteTick, LightDayTick, LightNightTick, HeavyReviewTick,
                DailyPlanTick, GlimpseTick]:
        meta = getattr(cls, "Meta", None)
        assert meta is not None and getattr(meta, "transient", False), (
            f"{cls.__name__} should declare Meta.transient = True"
        )


def test_glimpse_request_is_persisted():
    """GlimpseRequest is durable -> must NOT be transient."""
    from app.domain.life_dataflow import GlimpseRequest
    meta = getattr(GlimpseRequest, "Meta", None)
    if meta is not None:
        assert not getattr(meta, "transient", False), (
            "GlimpseRequest goes through .durable() — must not be transient"
        )


def test_glimpse_request_key_is_request_id():
    from app.domain.life_dataflow import GlimpseRequest
    assert key_fields(GlimpseRequest) == ("request_id",)


def test_other_business_requests_keyed_by_persona():
    from app.domain.life_dataflow import (
        LifeTickRequest, VoiceRequest, LightReviewRequest, HeavyReviewRequest,
        GlimpseTickRequest, DailyPlanRequest,
    )
    for cls in [LifeTickRequest, VoiceRequest, LightReviewRequest,
                HeavyReviewRequest, GlimpseTickRequest, DailyPlanRequest]:
        assert key_fields(cls) == ("persona_id",), f"{cls.__name__} key wrong"


def test_shared_daily_context_keyed_by_date():
    from app.domain.life_dataflow import SharedDailyContext
    assert key_fields(SharedDailyContext) == ("target_date",)


def test_life_state_changed_keyed_by_persona():
    from app.domain.life_dataflow import LifeStateChanged
    assert key_fields(LifeStateChanged) == ("persona_id",)
