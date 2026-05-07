"""API routes — health only.

All admin / API endpoints are now declared via Source.http in
app/wiring/admin.py and registered automatically by
register_http_sources(app) called from main.py.
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
