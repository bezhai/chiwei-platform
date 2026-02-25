"""消息内容解析器

解析 v2 JSON 格式的消息内容: {"v": 2, "text": "...", "items": [...]}

使用方式:
    parsed = parse_content(raw)
    parsed.render()                           # 纯文本（跳过图片）
    parsed.render(image_fn=lambda i, k: ...) # 自定义图片渲染
    parsed.image_keys                         # 图片 key 列表
    parsed.items                              # 原始 items
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# image_fn 类型: (图片在当前消息中的序号, image_key) -> 渲染文本
ImageRenderFn = Callable[[int, str], str]


@dataclass
class ParsedContent:
    """v2 消息解析结果"""

    text: str  # v2 原始 text 字段（含 markdown 标记，一般不直接使用）
    image_keys: list[str]  # 图片 key 列表
    items: list[dict] = field(default_factory=list)  # v2 原始 items
    mentions: list[dict] = field(
        default_factory=list
    )  # @提及的用户列表 [{user_id, name}]

    def render(self, image_fn: ImageRenderFn | None = None) -> str:
        """从 items 结构化渲染文本

        直接基于 items 数组构建，避免对 text 字段做字符串替换。
        渲染规则与 TypeScript 侧 MessageContentUtils.toMarkdown() 一致。

        Args:
            image_fn: 图片渲染回调 (序号, key) -> 显示文本。
                      为 None 时跳过图片。
        """
        if not self.items:
            return self.text

        parts: list[str] = []
        img_idx = 0

        for item in self.items:
            item_type = item.get("type")
            value = item.get("value", "")
            meta = item.get("meta", {})

            if item_type == "text":
                parts.append(value)
            elif item_type == "image":
                if image_fn:
                    parts.append(image_fn(img_idx, value))
                    img_idx += 1
            elif item_type == "sticker":
                parts.append("[表情包]")
            elif item_type == "media":
                name = meta.get("file_name")
                parts.append(f"[视频: {name}]" if name else "[视频]")
            elif item_type == "file":
                name = meta.get("file_name")
                parts.append(f"[文件: {name}]" if name else "[文件]")
            elif item_type == "audio":
                parts.append("[语音]")
            elif item_type == "unsupported":
                parts.append(value)
            else:
                # 未知类型兜底：保留 value 避免内容丢失
                if value:
                    parts.append(value)

        return "".join(parts)


def parse_content(raw: str) -> ParsedContent:
    """解析 v2 格式的消息内容

    非 v2 格式的输入视为纯文本（不做图片提取）。
    """
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("v") == 2:
            text = data.get("text", "")
            items = data.get("items", [])
            image_keys = [
                item["value"] for item in items if item.get("type") == "image"
            ]
            mentions = data.get("mentions", [])
            return ParsedContent(
                text=text,
                image_keys=image_keys,
                items=items,
                mentions=mentions,
            )
    except (json.JSONDecodeError, TypeError):
        pass

    # 非 v2 格式，视为纯文本
    logger.debug("Non-v2 content format, treating as plain text")
    return ParsedContent(text=raw, image_keys=[])
