"""Runtime lane policy tests."""

from __future__ import annotations

from app.runtime.lane_policy import (
    classify_deployment_lane,
    normalize_deployment_lane,
    time_sources_enabled_by_default,
)


def test_normalize_deployment_lane_treats_missing_and_prod_as_prod() -> None:
    assert normalize_deployment_lane(None) is None
    assert normalize_deployment_lane("") is None
    assert normalize_deployment_lane("prod") is None
    assert normalize_deployment_lane("ppe-refactor") == "ppe-refactor"


def test_classify_deployment_lane_matches_paas_lane_contract() -> None:
    assert classify_deployment_lane(None) == "prod"
    assert classify_deployment_lane("prod") == "prod"
    assert classify_deployment_lane("blue") == "prod"
    assert classify_deployment_lane("coe-agent") == "coe"
    assert classify_deployment_lane("ppe-refactor") == "ppe"
    assert classify_deployment_lane("dev") == "unknown"


def test_time_sources_default_only_prod_class_runs() -> None:
    assert time_sources_enabled_by_default(None)
    assert time_sources_enabled_by_default("prod")
    assert time_sources_enabled_by_default("blue")
    assert not time_sources_enabled_by_default("coe-agent")
    assert not time_sources_enabled_by_default("ppe-refactor")
    assert not time_sources_enabled_by_default("dev")


def test_time_sources_explicit_override_enables_test_lane() -> None:
    assert time_sources_enabled_by_default(
        "ppe-refactor",
        enable_override="1",
    )
