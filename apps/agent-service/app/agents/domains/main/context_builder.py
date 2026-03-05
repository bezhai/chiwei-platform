"""上下文构建模块

负责构建聊天上下文，包括历史消息获取、格式化和结构化。
"""

import asyncio
import logging

from langchain.messages import AIMessage, HumanMessage

from app.agents.infra.langfuse_client import get_prompt
from app.clients.image_client import image_client
from app.services.quick_search import QuickSearchResult, quick_search
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)


async def build_chat_context(
    message_id: str, limit: int = 10
) -> tuple[list[HumanMessage | AIMessage], list[str], str, str, str, str, str]:
    """构建聊天上下文，支持私聊和群聊使用不同组装策略

    群聊: 按回复链分组，组装成一条 HumanMessage
    私聊: 直接使用历史消息组装成 HumanMessage 和 AIMessage 列表

    Args:
        message_id: 触发消息的ID
        limit: 获取的历史消息数量限制

    Returns:
        tuple: (消息列表, 图片URL列表, chat_id, 触发用户名, 聊天类型, 触发用户ID, 群聊名称)
    """
    # L1: 使用 quick_search 拉取近期历史
    l1_results = await quick_search(message_id=message_id, limit=limit)

    if not l1_results:
        logger.warning(f"No results found for message_id: {message_id}")
        return [], [], "", "", "p2p", "", ""

    chat_type = l1_results[-1].chat_type or "p2p"  # 默认私聊

    # 1. 从content里批量提取图片keys, 获得图片key到URL的映射
    all_image_keys: list[tuple[str, str, str]] = []  # (key, message_id, role)

    # 提取所有图片keys
    for msg in l1_results:
        parsed = parse_content(msg.content)
        for key in parsed.image_keys:
            all_image_keys.append((key, msg.message_id, msg.role))

    # 批量处理所有图片，建立key到URL的映射
    image_key_to_url: dict[str, str] = {}
    if all_image_keys:
        image_tasks = [
            image_client.process_image(key, msg_id if role == "user" else None)
            for key, msg_id, role in all_image_keys
        ]
        image_results = await asyncio.gather(*image_tasks, return_exceptions=True)

        # 建立映射关系，失败的图片不加入映射
        for i, result in enumerate(image_results):
            key, msg_id, _ = all_image_keys[i]
            if isinstance(result, str) and result:
                image_key_to_url[key] = result
            else:
                logger.warning(f"图片处理失败: key={key}, message_id={msg_id}")

    # 2. 根据chat_type使用不同策略组装消息列表
    if chat_type == "group":
        # 群聊：按回复链分组，组装成一条HumanMessage
        messages = await _build_group_messages(l1_results, message_id, image_key_to_url)
    else:
        # 私聊：直接组装成HumanMessage和AIMessage列表
        messages = await _build_p2p_messages(l1_results, image_key_to_url)

    # 提取所有成功的图片URL列表（用于context）
    image_urls = list(image_key_to_url.values())

    # 提取触发消息的用户名和用户ID（最后一条消息即为触发消息）
    trigger_username = l1_results[-1].username or ""
    trigger_user_id = l1_results[-1].user_id or ""

    # 提取群聊名称
    chat_name = l1_results[-1].chat_name or ""

    return (
        messages,
        image_urls,
        l1_results[0].chat_id or "",
        trigger_username,
        chat_type,
        trigger_user_id,
        chat_name,
    )


def _extract_reply_chain(
    messages: list[QuickSearchResult], trigger_id: str
) -> tuple[list[QuickSearchResult], list[QuickSearchResult]]:
    """从触发消息向上追溯 reply_message_id，提取回复链

    Returns:
        (chain_messages, other_messages) - 均按时间升序
    """
    msg_map = {msg.message_id: msg for msg in messages}

    chain_ids: set[str] = set()
    current_id: str | None = trigger_id
    while current_id and current_id in msg_map:
        chain_ids.add(current_id)
        current_id = msg_map[current_id].reply_message_id

    chain = [m for m in messages if m.message_id in chain_ids]
    other = [m for m in messages if m.message_id not in chain_ids]
    return chain, other


async def _build_group_messages(
    messages: list[QuickSearchResult],
    trigger_id: str,
    image_key_to_url: dict[str, str],
) -> list[HumanMessage | AIMessage]:
    """构建群聊消息列表

    按回复链分组：回复链内消息有序排列，其他消息作为背景上下文。

    Args:
        messages: 消息列表
        trigger_id: 触发消息的ID
        image_key_to_url: 图片key到URL的映射

    Returns:
        包含一条 HumanMessage 的列表
    """
    chain, other = _extract_reply_chain(messages, trigger_id)

    # 全局图片计数器，跨回复链和其他消息连续编号
    image_count = 0

    # 格式化回复链消息（有序链，不需要编号和回复标记）
    chain_lines = []
    for msg in chain:
        time_str = msg.create_time.strftime("%H:%M:%S")
        username = msg.username or "未知用户"
        parsed = parse_content(msg.content)
        base = image_count
        text = parsed.render(image_fn=lambda i, _key: f"【图片{base + i + 1}】")
        image_count += len(parsed.image_keys)
        marker = " ⭐" if msg.message_id == trigger_id else ""
        chain_lines.append(f"[{time_str}] {username}: {text}{marker}")

    # 格式化其他消息（简略背景）
    other_lines = []
    for msg in other:
        time_str = msg.create_time.strftime("%H:%M:%S")
        username = msg.username or "未知用户"
        parsed = parse_content(msg.content)
        base = image_count
        text = parsed.render(image_fn=lambda i, _key: f"【图片{base + i + 1}】")
        image_count += len(parsed.image_keys)
        other_lines.append(f"[{time_str}] {username}: {text}")

    # 用 context_builder 模板组装
    user_content = get_prompt("context_builder").compile(
        reply_chain="\n".join(chain_lines) if chain_lines else "（无回复链）",
        other_messages="\n".join(other_lines) if other_lines else "（无其他消息）",
    )

    content_blocks: list = [{"type": "text", "text": user_content}]
    for url in image_key_to_url.values():
        content_blocks.append({"type": "image", "url": url})

    return [HumanMessage(content_blocks=content_blocks)]  # type: ignore


async def _build_p2p_messages(
    messages: list[QuickSearchResult], image_key_to_url: dict[str, str]
) -> list[HumanMessage | AIMessage]:
    """构建私聊消息列表

    直接将历史消息组装成 HumanMessage 和 AIMessage 列表

    Args:
        messages: 消息列表
        image_key_to_url: 图片key到URL的映射

    Returns:
        HumanMessage 和 AIMessage 的列表
    """
    result: list[HumanMessage | AIMessage] = []

    for msg in messages:
        # 提取消息中的图片keys和纯文本（render() 跳过图片，图片作为独立 content block 发送）
        parsed = parse_content(msg.content)
        image_keys = parsed.image_keys
        text_content = parsed.render()

        # 构建消息内容块
        content_blocks: list = []

        # 添加纯文本内容（不包含元信息前缀，避免 LLM 模仿格式）
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        # 添加该消息对应的图片（只添加成功获取URL的图片）
        for key in image_keys:
            if key in image_key_to_url:
                content_blocks.append({"type": "image", "url": image_key_to_url[key]})
            else:
                logger.warning(
                    f"消息中的图片未找到URL: key={key}, message_id={msg.message_id}"
                )

        # 如果没有任何内容，跳过该消息
        if not content_blocks:
            continue

        # 根据 role 创建对应的消息类型
        if msg.role == "assistant":
            result.append(AIMessage(content_blocks=content_blocks))  # type: ignore
        else:  # user 或其他角色都作为 HumanMessage
            result.append(HumanMessage(content_blocks=content_blocks))  # type: ignore

    return result


