"""Lane router — module-level ``lane_router`` instance."""

from __future__ import annotations

import os

from inner_shared.lane_router import LaneRouter

from app.api.middleware import get_lane

lane_router = LaneRouter(
    registry_url=os.getenv("REGISTRY_URL", "http://lite-registry:8080"),
    lane_provider=get_lane,
)
