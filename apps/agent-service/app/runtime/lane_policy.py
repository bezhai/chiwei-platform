"""Runtime lane policy.

PaaS owns lane naming, but agent-service still needs one local decision:
time-based sources (cron / interval) must not run by default in test lanes.
Those lanes may share prod data, so a second cron runner can create real
business side effects.
"""

from __future__ import annotations

import os
from typing import Literal, TypeAlias

LaneClass: TypeAlias = Literal["prod", "coe", "ppe", "unknown"]

_UNSET = object()


def normalize_deployment_lane(lane: str | None) -> str | None:
    """Normalize deployment lane; None means prod."""
    if not lane or lane == "prod":
        return None
    return lane


def current_deployment_lane() -> str | None:
    """Read the process deployment lane from env.

    This intentionally ignores request context. Source loops are process-level
    background work, so the only relevant lane is the Deployment's ``LANE``.
    """
    return normalize_deployment_lane(os.getenv("LANE"))


def classify_deployment_lane(lane: str | None) -> LaneClass:
    """Mirror the PaaS lane contract for runtime safety decisions."""
    normalized = normalize_deployment_lane(lane)
    if normalized is None or normalized == "blue":
        return "prod"
    if normalized.startswith("coe-"):
        return "coe"
    if normalized.startswith("ppe-"):
        return "ppe"
    return "unknown"


def time_sources_enabled_by_default(
    lane: str | None | object = _UNSET,
    *,
    enable_override: str | None | object = _UNSET,
) -> bool:
    """Return whether cron / interval sources should run by default.

    ``DATAFLOW_ENABLE_TIME_SOURCES=1`` is the explicit escape hatch for a
    coe/ppe verification that is intentionally testing cron behavior.
    """
    actual_lane = current_deployment_lane() if lane is _UNSET else lane
    override = (
        os.getenv("DATAFLOW_ENABLE_TIME_SOURCES")
        if enable_override is _UNSET
        else enable_override
    )
    if override == "1":
        return True
    return classify_deployment_lane(actual_lane) == "prod"
