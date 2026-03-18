import asyncio
import io
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    GetMessageResourceRequest,
    GetImageRequest,
)
from sqlalchemy import select

from app.infrastructure.database import get_session_factory
from app.orm.bot_config import BotConfig

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF = (0.5, 1.0, 2.0)


def _extract_error(response) -> str:
    """Extract error details from Lark SDK response, including raw HTTP info."""
    parts = [f"code={response.code}", f"msg={response.msg}"]
    if response.raw:
        parts.append(f"http_status={response.raw.status_code}")
        try:
            body = response.raw.content[:500].decode("utf-8", errors="replace")
            parts.append(f"body={body}")
        except Exception:
            pass
    return ", ".join(parts)

# bot_name -> lark.Client
_clients: dict[str, lark.Client] = {}


async def init_lark_clients() -> None:
    """Load all active bot configs from DB and create Lark SDK clients."""
    session_factory = get_session_factory()
    if session_factory is None:
        logger.warning("Database not configured, Lark clients disabled")
        return

    async with session_factory() as session:
        result = await session.execute(
            select(BotConfig).where(BotConfig.is_active.is_(True))
        )
        bots = result.scalars().all()

    for bot in bots:
        _clients[bot.bot_name] = lark.Client.builder() \
            .app_id(bot.app_id) \
            .app_secret(bot.app_secret) \
            .build()
        logger.info(f"Lark client initialized for bot: {bot.bot_name}")

    logger.info(f"Loaded {len(_clients)} Lark bot clients")


def get_client(bot_name: str) -> lark.Client:
    client = _clients.get(bot_name)
    if client is None:
        raise ValueError(f"Unknown bot: {bot_name}")
    return client


async def download_message_resource(bot_name: str, message_id: str, file_key: str) -> bytes:
    """Download a resource (image/file) attached to a message, with retry."""
    client = get_client(bot_name)
    last_error = None
    for attempt in range(_MAX_RETRIES):
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("image") \
            .build()
        response = await client.im.v1.message_resource.aget(request)
        if response.success():
            return response.file.read()
        error_detail = _extract_error(response)
        last_error = error_detail
        logger.warning(
            "Download resource attempt %d/%d failed: %s (file_key=%s)",
            attempt + 1, _MAX_RETRIES, error_detail, file_key,
        )
        if attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(_RETRY_BACKOFF[attempt])
    raise RuntimeError(f"Download resource failed after {_MAX_RETRIES} retries: {last_error}")


async def download_image(bot_name: str, image_key: str) -> bytes:
    """Download an image by image_key, with retry."""
    client = get_client(bot_name)
    last_error = None
    for attempt in range(_MAX_RETRIES):
        request = GetImageRequest.builder() \
            .image_key(image_key) \
            .build()
        response = await client.im.v1.image.aget(request)
        if response.success():
            return response.file.read()
        error_detail = _extract_error(response)
        last_error = error_detail
        logger.warning(
            "Download image attempt %d/%d failed: %s (image_key=%s)",
            attempt + 1, _MAX_RETRIES, error_detail, image_key,
        )
        if attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(_RETRY_BACKOFF[attempt])
    raise RuntimeError(f"Download image failed after {_MAX_RETRIES} retries: {last_error}")


async def upload_image(bot_name: str, image_data: bytes) -> str:
    """Upload an image to Lark, return image_key."""
    client = get_client(bot_name)
    request = CreateImageRequest.builder() \
        .request_body(
            CreateImageRequestBody.builder()
            .image_type("message")
            .image(io.BytesIO(image_data))
            .build()
        ) \
        .build()
    response = await client.im.v1.image.acreate(request)
    if not response.success():
        raise RuntimeError(f"Upload image failed: {response.code} {response.msg}")
    return response.data.image_key
