from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def callback_url_allowed(
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
    allowed_networks: tuple[str, ...],
) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname
    if host in allowed_hosts:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(network) for network in allowed_networks)


async def post_callback(url: str, payload: dict, *, auth_token: str, timeout_seconds: float) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
