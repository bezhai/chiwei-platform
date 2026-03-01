import base64
import logging

from app.infrastructure import lark_client, redis_client, tos_client
from app.infrastructure.redis_lock import RedisLock
from app.services.image_service import process_image

logger = logging.getLogger(__name__)

_UPLOAD_CACHE_TTL = 7 * 24 * 60 * 60  # 7 days
_URL_CACHE_TTL = 10 * 60  # 10 minutes


async def process_image_pipeline(
    file_key: str, message_id: str | None, bot_name: str
) -> dict:
    """
    Image pipeline: download from Lark -> compress -> upload to TOS -> return URL.
    Two-layer cache: upload cache (7d) + URL cache (10min).
    """
    # Layer 1: check if file already uploaded
    upload_cache_key = f"image_upload:{file_key}"
    file_name = await redis_client.redis_get(upload_cache_key)

    if not file_name:
        # Acquire distributed lock to prevent concurrent uploads
        async with RedisLock(f"image_upload_lock:{file_key}", ttl=60, timeout=30):
            # Double-check after acquiring lock
            file_name = await redis_client.redis_get(upload_cache_key)
            if not file_name:
                # Download image from Lark
                if message_id:
                    image_bytes = await lark_client.download_message_resource(
                        bot_name, message_id, file_key
                    )
                else:
                    image_bytes = await lark_client.download_image(bot_name, file_key)

                # Compress: 1440x1440, JPEG q80
                compressed, _, _ = process_image(
                    image_bytes, max_width=1440, max_height=1440, quality=80, format="JPEG"
                )

                # Upload to TOS
                file_name = f"temp/{file_key}.jpg"
                await tos_client.upload_file(file_name, compressed)

                # Write layer 1 cache
                await redis_client.redis_set_with_expire(upload_cache_key, file_name, _UPLOAD_CACHE_TTL)
                logger.info(f"Image uploaded: {file_key} -> {file_name}")
    else:
        logger.debug(f"Image already uploaded (cache hit): {file_key}")

    # Layer 2: get pre-signed URL with cache
    url_cache_key = f"image_url:{file_name}"
    url = await redis_client.redis_get(url_cache_key)
    if not url:
        url = await tos_client.get_file_url(file_name)
        await redis_client.redis_set_with_expire(url_cache_key, url, _URL_CACHE_TTL)

    return {"url": url, "file_key": file_key}


async def upload_base64_image(base64_data: str, bot_name: str) -> dict:
    """
    Upload a base64 image to Lark, return image_key.
    """
    # Strip data:image/...;base64, prefix
    if "," in base64_data:
        base64_data = base64_data.split(",", 1)[1]

    image_bytes = base64.b64decode(base64_data)
    image_key = await lark_client.upload_image(bot_name, image_bytes)
    logger.info(f"Base64 image uploaded, image_key: {image_key}")
    return {"image_key": image_key}
