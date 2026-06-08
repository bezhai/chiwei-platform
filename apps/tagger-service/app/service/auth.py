from __future__ import annotations

import secrets


def bearer_token_from_header(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, sep, token = authorization.partition(" ")
    if not sep or scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def bearer_token_allowed(authorization: str | None, allowed_tokens: tuple[str, ...]) -> bool:
    provided = bearer_token_from_header(authorization)
    if provided is None or not allowed_tokens:
        return False
    return any(secrets.compare_digest(provided, token) for token in allowed_tokens)
