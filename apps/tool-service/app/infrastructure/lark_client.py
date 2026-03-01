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
    """Download a resource (image/file) attached to a message."""
    client = get_client(bot_name)
    request = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(file_key) \
        .type("image") \
        .build()
    response = client.im.v1.message_resource.get(request)
    if not response.success():
        raise RuntimeError(f"Download resource failed: {response.code} {response.msg}")
    return response.file.read()


async def download_image(bot_name: str, image_key: str) -> bytes:
    """Download an image by image_key."""
    client = get_client(bot_name)
    request = GetImageRequest.builder() \
        .image_key(image_key) \
        .build()
    response = client.im.v1.image.get(request)
    if not response.success():
        raise RuntimeError(f"Download image failed: {response.code} {response.msg}")
    return response.file.read()


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
    response = client.im.v1.image.create(request)
    if not response.success():
        raise RuntimeError(f"Upload image failed: {response.code} {response.msg}")
    return response.data.image_key
