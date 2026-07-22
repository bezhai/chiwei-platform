"""Typed transport capability for persona-review Feishu notifications."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

_WEBHOOK_TIMEOUT_SECONDS = 10.0


class PersonaNotificationOutcome(StrEnum):
    SUCCESS = "success"
    HTTP_ERROR = "http_error"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class PersonaNotificationResult:
    """Parsed webhook outcome with no raw HTTP response escaping the capability."""

    outcome: PersonaNotificationOutcome
    status_code: int
    response_preview: str = ""
    provider_code: Any = 0
    provider_message: Any = None

    @classmethod
    def success(cls, *, status_code: int) -> PersonaNotificationResult:
        return cls(PersonaNotificationOutcome.SUCCESS, status_code)

    @classmethod
    def http_error(
        cls, *, status_code: int, response_preview: str
    ) -> PersonaNotificationResult:
        return cls(
            PersonaNotificationOutcome.HTTP_ERROR,
            status_code,
            response_preview=response_preview,
        )

    @classmethod
    def provider_error(
        cls,
        *,
        status_code: int,
        provider_code: Any,
        provider_message: Any,
    ) -> PersonaNotificationResult:
        return cls(
            PersonaNotificationOutcome.PROVIDER_ERROR,
            status_code,
            provider_code=provider_code,
            provider_message=provider_message,
        )


class PersonaNotificationCallFailed(RuntimeError):
    """Transport or response parsing failed before a typed outcome was available."""


async def send_persona_notification(
    *, url: str, text: str
) -> PersonaNotificationResult:
    """POST one Feishu text message and map its HTTP/provider response."""
    try:
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                json={"msg_type": "text", "content": {"text": text}},
            )
    except httpx.HTTPError as exc:
        raise PersonaNotificationCallFailed(
            f"webhook transport failed ({type(exc).__name__})"
        ) from exc

    if not response.is_success:
        return PersonaNotificationResult.http_error(
            status_code=response.status_code,
            response_preview=response.text[:200],
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise PersonaNotificationCallFailed("webhook response is not JSON") from exc
    if not isinstance(body, dict):
        raise PersonaNotificationCallFailed("webhook response is not an object")

    provider_code = body.get("code", 0)
    if provider_code != 0:
        return PersonaNotificationResult.provider_error(
            status_code=response.status_code,
            provider_code=provider_code,
            provider_message=body.get("msg"),
        )
    return PersonaNotificationResult.success(status_code=response.status_code)
