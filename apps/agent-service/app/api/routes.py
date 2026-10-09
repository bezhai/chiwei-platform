"""API routes — health only.

The admin endpoints are routes of the app's plugins (``app/plugins``); the plugin host puts
them on the app when it starts (``app.main``'s lifespan).
"""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", tags=["Health"])
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "service": "agent-service",
        "version": os.environ.get("GIT_SHA", "unknown"),
    }
