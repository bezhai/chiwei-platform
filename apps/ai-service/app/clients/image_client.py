"""
Main-server图片处理客户端
"""

import base64
import logging

import httpx

from app.config.config import settings
from app.utils.middlewares.trace import get_app_name, get_trace_id

logger = logging.getLogger(__name__)


class ImageProcessClient:
    """Main-server图片处理客户端"""

    def __init__(self):
        self.base_url = settings.main_server_base_url
        self.timeout = settings.main_server_timeout

    async def process_image(
        self, file_key: str, message_id: str | None, bot_name: str | None = None
    ) -> str | None:
        """
        处理图片，返回图片URL

        Args:
            file_key: 图片文件key
            message_id: 消息ID
            bot_name: 机器人名称（用于多 bot 场景）

        Returns:
            str: 图片URL，如果失败返回None
        """
        if not self.base_url:
            logger.warning("Main-server base URL未配置")
            return None

        # 优先使用传入的 bot_name，否则从上下文获取
        app_name = bot_name or get_app_name() or ""

        try:
            request_data = {"message_id": message_id, "file_key": file_key}

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/image/process",
                    json=request_data,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {settings.inner_http_secret}",
                        "X-Trace-Id": get_trace_id() or "",
                        "X-App-Name": app_name,
                    },
                )

                response.raise_for_status()
                data = response.json()

                if data.get("success") and data.get("data"):
                    logger.info(f"图片处理成功: {file_key} -> {data['data']['url']}")
                    return data["data"]["url"]
                else:
                    logger.error(f"图片处理失败: {data.get('message', '未知错误')}")
                    return None

        except httpx.TimeoutException:
            logger.warning(f"图片处理超时: {self.timeout}秒")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(
                f"图片处理HTTP错误: {e.response.status_code} - {e.response.text}"
            )
            return None
        except Exception as e:
            logger.error(f"调用图片处理接口失败: {str(e)}")
            return None

    async def upload_base64_image(
        self, base64_data: str, bot_name: str | None = None
    ) -> str | None:
        """
        上传base64图片到飞书，返回image_key

        Args:
            base64_data: base64图片数据，需要包含data:image/...;base64,前缀
            bot_name: 机器人名称（用于多 bot 场景）

        Returns:
            str: image_key，如果失败返回None
        """
        if not self.base_url:
            logger.warning("Main-server base URL未配置")
            return None

        # 优先使用传入的 bot_name，否则从上下文获取
        app_name = bot_name or get_app_name() or ""

        try:
            # logger.info(f"上传base64图片到飞书，base64_data: {base64_data}")
            request_data = {"base64_data": base64_data}

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/image/upload-base64",
                    json=request_data,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {settings.inner_http_secret}",
                        "X-Trace-Id": get_trace_id() or "",
                        "X-App-Name": app_name,
                    },
                )

                response.raise_for_status()
                data = response.json()

                if data.get("success") and data.get("data"):
                    logger.info(
                        f"base64图片上传成功，获得image_key: {data['data']['image_key']}"
                    )
                    return data["data"]["image_key"]
                else:
                    logger.error(
                        f"base64图片上传失败: {data.get('message', '未知错误')}"
                    )
                    return None

        except httpx.TimeoutException:
            logger.warning(f"base64图片上传超时: {self.timeout}秒")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(
                f"base64图片上传HTTP错误: {e.response.status_code} - {e.response.text}"
            )
            return None
        except Exception as e:
            logger.error(f"调用base64图片上传接口失败: {str(e)}")
            return None

    async def download_image_as_base64(
        self, file_key: str, message_id: str | None, bot_name: str | None = None
    ) -> str | None:
        """
        下载图片并转换为Base64格式

        Args:
            file_key: 图片文件key
            message_id: 消息ID
            bot_name: 机器人名称（���于多 bot 场景）

        Returns:
            str: Base64格式图片 data:image/{format};base64,{base64_data}
            失败返回None
        """
        try:
            # 1. 获取图片URL
            image_url = await self.process_image(file_key, message_id, bot_name)
            if not image_url:
                logger.warning(f"无法获取图片URL: {file_key}")
                return None

            # 2. 下载图片内容
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(image_url)
                response.raise_for_status()

                # 3. 获取图片格式
                content_type = response.headers.get("content-type", "image/jpeg")
                image_format = content_type.split("/")[-1].split(";")[0].lower()

                # 支持的格式映射
                format_map = {
                    "jpeg": "jpeg",
                    "jpg": "jpeg",
                    "png": "png",
                    "gif": "gif",
                    "webp": "webp",
                    "bmp": "bmp",
                }
                image_format = format_map.get(image_format, "jpeg")

                # 4. 转换为Base64
                image_bytes = response.content
                base64_str = base64.b64encode(image_bytes).decode("utf-8")

                # 5. 返回完整格式
                result = f"data:image/{image_format};base64,{base64_str}"
                logger.info(
                    f"图片下载并转换为Base64成功: {file_key}, 格式: {image_format}"
                )
                return result

        except httpx.TimeoutException:
            logger.warning(f"图片下载超时: {file_key}")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"图片下载HTTP错误: {e.response.status_code} - {file_key}")
            return None
        except Exception as e:
            logger.error(f"图片下载转换失败: {file_key} - {str(e)}")
            return None


# 全局单例
image_client = ImageProcessClient()
