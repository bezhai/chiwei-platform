import asyncio
import logging

import tos
from tos.enum import HttpMethodType

from app.config.config import settings

logger = logging.getLogger(__name__)

_client: tos.TosClientV2 | None = None


def _get_sync_client() -> tos.TosClientV2:
    global _client
    if _client is None:
        _client = tos.TosClientV2(
            ak=settings.tos_access_key_id,
            sk=settings.tos_access_key_secret,
            endpoint=settings.tos_endpoint,
            region=settings.tos_region,
        )
    return _client


def _bucket() -> str:
    if not settings.tos_bucket:
        raise RuntimeError("TOS bucket not configured")
    return settings.tos_bucket


async def upload_file(file_name: str, data: bytes) -> None:
    def _upload():
        _get_sync_client().put_object(bucket=_bucket(), key=file_name, content=data)
        logger.debug(f"Uploaded to TOS: {file_name}")

    await asyncio.to_thread(_upload)


async def get_file_url(file_name: str) -> str:
    def _get_url():
        return _get_sync_client().pre_signed_url(
            http_method=HttpMethodType.Http_Method_Get,
            bucket=_bucket(),
            key=file_name,
            expires=int(1.5 * 60 * 60),  # 1.5 hours
        ).signed_url

    return await asyncio.to_thread(_get_url)


async def get_file(file_name: str) -> bytes:
    def _get():
        resp = _get_sync_client().get_object(bucket=_bucket(), key=file_name)
        return resp.read()

    return await asyncio.to_thread(_get)
