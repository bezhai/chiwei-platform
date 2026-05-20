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

_LARK_CHANNEL = "lark"

# bot_name -> lark.Client
_clients: dict[str, lark.Client] = {}


def lark_credentials_from_row(bot) -> tuple[str, str] | None:
    """把一条 bot_config 记录解释成 (app_id, app_secret)。

    bot_config 多 channel 化后飞书凭据迁进 credentials JSONB、旧裸列已删。
    tool-service 只建飞书 SDK client：非 lark 记录返回 None（跳过，不是
    tool-service 的事）；lark 记录缺凭据明确抛错而不是静默放过——凭据缺失
    静默会让飞书鉴权在运行期出诡异错。
    """
    if getattr(bot, "channel", _LARK_CHANNEL) != _LARK_CHANNEL:
        return None
    creds = bot.credentials
    if not isinstance(creds, dict):
        raise ValueError(
            f"lark bot {bot.bot_name!r} has no credentials JSONB payload"
        )
    out = []
    for field in ("app_id", "app_secret"):
        v = creds.get(field)
        if not isinstance(v, str) or not v:
            raise ValueError(
                f"lark bot {bot.bot_name!r} missing required credential {field!r}"
            )
        out.append(v)
    return out[0], out[1]


async def init_lark_clients() -> None:
    """Load all active lark bot configs from DB and create Lark SDK clients."""
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
        creds = lark_credentials_from_row(bot)
        if creds is None:
            continue
        app_id, app_secret = creds
        _clients[bot.bot_name] = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
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
