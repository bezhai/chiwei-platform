"""上下文构建模块

负责构建聊天上下文，包括历史消息获取、格式化和结构化。
图片统一注册到 ImageRegistry，文本中用 @N.png 引用。
"""

import asyncio
import contextvars
import logging

from langchain.messages import AIMessage, HumanMessage
from sqlalchemy import select

from app.agents.infra.langfuse_client import get_prompt
from app.clients.image_client import image_client
from app.clients.image_registry import ImageRegistry
from app.orm.base import AsyncSessionLocal
from app.orm.models import ConversationMessage
from app.services.content_parser import parse_content, update_tos_files
from app.services.download_permission import check_group_allows_download
from app.services.quick_search import QuickSearchResult, quick_search

logger = logging.getLogger(__name__)

PROACTIVE_USER_ID = "__proactive__"

_is_proactive_var: contextvars.ContextVar[bool] = contextvars.ContextVar("is_proactive", default=False)
_proactive_stimulus_var: contextvars.ContextVar[str] = contextvars.ContextVar("proactive_stimulus", default="")


async def build_chat_context(
    message_id: str,
    current_persona_id: str = "",
    limit: int = 10,
) -> tuple[list[HumanMessage | AIMessage], ImageRegistry | None, str, str, str, str, str, list[str]]:
    """构建聊天上下文，支持私聊和群聊使用不同组装策略

    图片处理流程:
    1. Feishu image_key → process_image → TOS URL
    2. TOS URL → ImageRegistry.register → N.png
    3. 文本中用 @N.png 引用图片

    Returns:
        tuple: (消息列表, ImageRegistry, chat_id, 触发用户名, 聊天类型, 触发用户ID, 群聊名称, 回复链用户ID列表)
    """
    # L1: 使用 quick_search 拉取近期历史
    l1_results = await quick_search(message_id=message_id, limit=limit)

    if not l1_results:
        logger.warning(f"No results found for message_id: {message_id}")
        return [], None, "", "", "p2p", "", "", []

    chat_type = l1_results[-1].chat_type or "p2p"  # 默认私聊

    # --- Proactive: 过滤所有合成消息 ---
    proactive_msgs = [m for m in l1_results if m.user_id == PROACTIVE_USER_ID]
    is_proactive = len(proactive_msgs) > 0
    proactive_stimulus = ""
    proactive_target_id = ""

    if proactive_msgs:
        # 过滤掉所有合成消息
        l1_results = [m for m in l1_results if m.user_id != PROACTIVE_USER_ID]
        # 取最新一条合成消息的 stimulus
        latest_proactive = proactive_msgs[-1]
        proactive_stimulus = parse_content(latest_proactive.content).render()
        proactive_target_id = latest_proactive.reply_message_id or ""
        if not l1_results:
            logger.warning("proactive scan: no real messages found after filtering")
            return [], None, "", "", "group", "", "", []

    _is_proactive_var.set(is_proactive)
    _proactive_stimulus_var.set(proactive_stimulus)

    # 1. 从 content 里批量提取图片 keys，区分已缓存 vs 未缓存
    cached_keys: list[tuple[str, str]]  = []  # (image_key, tos_file)
    uncached_keys: list[tuple[str, str, str]] = []  # (image_key, message_id, role)
    for msg in l1_results:
        parsed = parse_content(msg.content)
        for key in parsed.image_keys:
            if key.startswith("@"):
                continue  # skip @N.png references (not real image keys)
            tos_file = parsed.tos_files.get(key)
            if tos_file:
                cached_keys.append((key, tos_file))
            else:
                uncached_keys.append((key, msg.message_id, msg.role))

    # 2a. 已缓存的图片：只签 URL，不走飞书下载
    image_key_to_url: dict[str, str] = {}
    image_key_to_file: dict[str, str] = {}  # 用于回写 DB
    if cached_keys:
        url_tasks = [image_client.get_url(tos_file) for _, tos_file in cached_keys]
        url_results = await asyncio.gather(*url_tasks, return_exceptions=True)
        for i, result in enumerate(url_results):
            key, tos_file = cached_keys[i]
            if isinstance(result, str) and result:
                image_key_to_url[key] = result
                image_key_to_file[key] = tos_file
            else:
                # URL 签名失败（文件可能被清理），回退到完整 pipeline
                uncached_keys.append((key, "", ""))
                logger.warning(f"TOS URL 签名失败，回退完整 pipeline: {key}")

    # 2b. 权限检查：禁止下载的群跳过飞书图片下载
    if uncached_keys:
        chat_id = l1_results[0].chat_id or ""
        if not await check_group_allows_download(chat_id, chat_type):
            logger.info(
                f"群 {chat_id} 不允许下载资源，跳过 {len(uncached_keys)} 张未缓存图片"
            )
            uncached_keys = []

    # 2c. 未缓存的图片：走完整 pipeline（飞书下载 → 压缩 → TOS）
    if uncached_keys:
        process_tasks = [
            image_client.process_image(key, msg_id if role == "user" else None)
            for key, msg_id, role in uncached_keys
        ]
        process_results = await asyncio.gather(*process_tasks, return_exceptions=True)

        for i, result in enumerate(process_results):
            key, msg_id, _ = uncached_keys[i]
            if isinstance(result, dict) and result:
                image_key_to_url[key] = result["url"]
                if result.get("file_name"):
                    image_key_to_file[key] = result["file_name"]
            else:
                logger.warning(f"图片处理失败: key={key}, message_id={msg_id}")

    # 2c. 将新获取的 tos_file 回写到消息 content（后台执行，不阻塞）
    if image_key_to_file:
        asyncio.create_task(_persist_tos_files(l1_results, image_key_to_file))

    # 3. 注册所有图片到 ImageRegistry
    registry = ImageRegistry(message_id)
    image_key_to_filename: dict[str, str] = {}
    if image_key_to_url:
        keys_ordered = list(image_key_to_url.keys())
        urls_ordered = [image_key_to_url[k] for k in keys_ordered]
        filenames = await registry.register_batch(urls_ordered)
        for key, filename in zip(keys_ordered, filenames, strict=False):
            image_key_to_filename[key] = filename

    # 提取触发消息的用户名和用户ID
    if is_proactive:
        trigger_username = ""
        trigger_user_id = ""
        chat_name = l1_results[-1].chat_name or "" if l1_results else ""
        effective_trigger_id = proactive_target_id or (l1_results[-1].message_id if l1_results else message_id)
    else:
        trigger_username = l1_results[-1].username or ""
        trigger_user_id = l1_results[-1].user_id or ""
        chat_name = l1_results[-1].chat_name or ""
        effective_trigger_id = message_id

    # 4. 根据 chat_type 使用不同策略组装消息列表
    if chat_type == "group":
        messages = _build_group_messages(
            l1_results, effective_trigger_id, image_key_to_url, image_key_to_filename,
        )
    else:
        messages = _build_p2p_messages(
            l1_results, image_key_to_url, image_key_to_filename,
            current_persona_id=current_persona_id,
        )

    # 提取回复链中所有用户ID
    chain_user_ids = list({
        r.user_id for r in l1_results
        if r.role != "assistant" and r.user_id
    })

    return (
        messages,
        registry,
        l1_results[0].chat_id or "",
        trigger_username,
        chat_type,
        trigger_user_id,
        chat_name,
        chain_user_ids,
    )


async def _persist_tos_files(
    messages: list[QuickSearchResult], image_key_to_file: dict[str, str]
) -> None:
    """后台将 tos_file 回写到消息 content 的 image items 中。"""
    try:
        # 按 message_id 聚合需要更新的 image_key → file_name
        msg_updates: dict[str, dict[str, str]] = {}  # message_id → {key: file_name}
        for msg in messages:
            parsed = parse_content(msg.content)
            new_mappings = {}
            for key in parsed.image_keys:
                if key in image_key_to_file and key not in parsed.tos_files:
                    new_mappings[key] = image_key_to_file[key]
            if new_mappings:
                msg_updates[msg.message_id] = new_mappings

        if not msg_updates:
            return

        async with AsyncSessionLocal() as session:
            for mid, mapping in msg_updates.items():
                row = await session.scalar(
                    select(ConversationMessage).where(
                        ConversationMessage.message_id == mid
                    )
                )
                if row:
                    updated = update_tos_files(row.content, mapping)
                    if updated:
                        row.content = updated
            await session.commit()
            logger.info(f"tos_file 回写完成: {len(msg_updates)} 条消息")
    except Exception:
        logger.warning("tos_file 回写失败", exc_info=True)


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


def _build_group_messages(
    messages: list[QuickSearchResult],
    trigger_id: str,
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
) -> list[HumanMessage | AIMessage]:
    """构建群聊消息列表

    回复链内消息图片作为 content_blocks 发送（带 @N.png: 标签），
    其余消息只在文本里写 @N.png。
    """
    chain, other = _extract_reply_chain(messages, trigger_id)

    # 文本渲染: 用 @N.png 替代图片占位符
    def _image_fn(_i: int, key: str) -> str:
        fn = image_key_to_filename.get(key)
        return f"@{fn}" if fn else "[图片]"

    # 格式化回复链消息
    chain_lines = []
    for msg in chain:
        time_str = msg.create_time.strftime("%H:%M:%S")
        username = msg.username or "未知用户"
        parsed = parse_content(msg.content)
        text = parsed.render(image_fn=_image_fn)
        marker = " ⭐" if msg.message_id == trigger_id else ""
        chain_lines.append(f"[{time_str}] {username}: {text}{marker}")

    # 格式化其他消息
    other_lines = []
    for msg in other:
        time_str = msg.create_time.strftime("%H:%M:%S")
        username = msg.username or "未知用户"
        parsed = parse_content(msg.content)
        text = parsed.render(image_fn=_image_fn)
        other_lines.append(f"[{time_str}] {username}: {text}")

    # 用 context_builder 模板组装
    user_content = get_prompt("context_builder").compile(
        reply_chain="\n".join(chain_lines) if chain_lines else "（无回复链）",
        other_messages="\n".join(other_lines) if other_lines else "（无其他消息）",
    )

    # content_blocks: 文本 + 回复链图片（带 @N.png: 标签）
    content_blocks: list = [{"type": "text", "text": user_content}]

    # 只发送回复链消息中的图片作为 content_blocks
    for msg in chain:
        parsed = parse_content(msg.content)
        for key in parsed.image_keys:
            fn = image_key_to_filename.get(key)
            url = image_key_to_url.get(key)
            if fn and url:
                content_blocks.append({"type": "text", "text": f"@{fn}:"})
                content_blocks.append({"type": "image", "url": url})

    return [HumanMessage(content_blocks=content_blocks)]  # type: ignore


def _build_p2p_messages(
    messages: list[QuickSearchResult],
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
    current_persona_id: str = "",
) -> list[HumanMessage | AIMessage]:
    """构建私聊消息列表

    所有图片作为 content_blocks（带 @N.png: 标签）
    """
    result: list[HumanMessage | AIMessage] = []

    def _image_fn(_i: int, key: str) -> str:
        fn = image_key_to_filename.get(key)
        return f"@{fn}" if fn else "[图片]"

    for msg in messages:
        parsed = parse_content(msg.content)
        image_keys = parsed.image_keys
        text_content = parsed.render(image_fn=_image_fn)

        content_blocks: list = []

        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        # 添加图片 content_blocks（带 @N.png: 标签）
        for key in image_keys:
            fn = image_key_to_filename.get(key)
            url = image_key_to_url.get(key)
            if fn and url:
                content_blocks.append({"type": "text", "text": f"@{fn}:"})
                content_blocks.append({"type": "image", "url": url})
            elif not fn:
                logger.warning(
                    f"消息中的图片未注册: key={key}, message_id={msg.message_id}"
                )

        if not content_blocks:
            continue

        # 当前 persona 自己的消息 → AIMessage，其余 → HumanMessage
        msg_persona_id = getattr(msg, "persona_id", None)
        if msg_persona_id:
            is_self = msg.role == "assistant" and msg_persona_id == current_persona_id
        else:
            is_self = False
        if is_self:
            result.append(AIMessage(content_blocks=content_blocks))  # type: ignore
        else:
            result.append(HumanMessage(content_blocks=content_blocks))  # type: ignore

    return result
