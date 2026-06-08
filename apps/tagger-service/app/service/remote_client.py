from __future__ import annotations

import asyncio
from typing import Any

from app.service.results import tagger_error_row


class RemoteTaggerClient:
    def __init__(
        self,
        base_url: str | None,
        *,
        auth_token: str,
        timeout_seconds: float,
        retries: int,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.auth_token = auth_token
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    async def infer(self, paths: list[str]) -> dict[str, Any]:
        if not self.base_url:
            message = "RemoteTaggerUnavailable: TAGGER_REMOTE_URL is not configured"
            return {"rows": [tagger_error_row(path, message) for path in paths], "dups": []}

        import httpx

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    headers = {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {}
                    response = await client.post(
                        f"{self.base_url}/api/v1/tagger/infer",
                        headers=headers,
                        json={"paths": paths},
                    )
                    response.raise_for_status()
                    return response.json()
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        message = f"{type(last_error).__name__}: {last_error}"
        return {"rows": [tagger_error_row(path, message) for path in paths], "dups": []}
