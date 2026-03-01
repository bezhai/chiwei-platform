from fastapi import Depends, HTTPException, Request

from app.config.config import settings


async def verify_bearer_token(request: Request) -> None:
    """FastAPI dependency: verify Authorization: Bearer {token}."""
    if not settings.inner_http_secret:
        return  # Auth not configured, skip

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]
    if token != settings.inner_http_secret:
        raise HTTPException(status_code=403, detail="Invalid token")
