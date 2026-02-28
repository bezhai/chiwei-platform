"""
全局 LaneRouter 实例
"""

import os

from inner_shared.lane_router import LaneRouter

from app.utils.middlewares.trace import get_lane

lane_router = LaneRouter(
    registry_url=os.getenv("REGISTRY_URL", "http://lite-registry:8080"),
    lane_provider=get_lane,
)
