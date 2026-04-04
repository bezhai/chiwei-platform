"""一次性迁移脚本：从飞书 API 拉取每个 bot 所在的群，写入 bot_chat_presence 表

用法（在 agent-service 容器内，或配好 DATABASE_URL 的环境下）：
    python scripts/migrate_bot_chat_presence.py

流程：
    1. 从 bot_config 读取所有 active bot 的 app_id / app_secret
    2. 对每个 bot 获取 tenant_access_token
    3. 调 GET /open-apis/im/v1/chats 拉取 bot 所在的所有群（chat_status=normal）
    4. UPSERT 到 bot_chat_presence 表
    P2P 不处理，靠增量写入覆盖。
"""

import asyncio
import logging
import httpx

from app.orm.base import AsyncSessionLocal
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"


async def get_tenant_token(app_id: str, app_secret: str) -> str:
    """获取 tenant_access_token"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get_tenant_token failed: {data}")
        return data["tenant_access_token"]


async def list_bot_chats(token: str) -> list[dict]:
    """分页拉取 bot 所在的所有群聊（过滤 chat_status=normal，跳��� p2p）"""
    chats = []
    page_token = ""
    async with httpx.AsyncClient() as client:
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            resp = await client.get(
                f"{FEISHU_BASE}/im/v1/chats",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"list_chats failed: {data}")

            for item in data.get("data", {}).get("items", []):
                # 只要 normal 状态的群聊，跳过 p2p
                if item.get("chat_status") == "normal" and item.get("chat_mode") != "p2p":
                    chats.append({
                        "chat_id": item["chat_id"],
                        "name": item.get("name", ""),
                    })

            if not data.get("data", {}).get("has_more"):
                break
            page_token = data["data"]["page_token"]
    return chats


async def main():
    # 1. 读取所有 active bot
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT bot_name, app_id, app_secret FROM bot_config WHERE is_active = true")
        )
        bots = [{"bot_name": r[0], "app_id": r[1], "app_secret": r[2]} for r in result.fetchall()]

    logger.info("Found %d active bots: %s", len(bots), [b["bot_name"] for b in bots])

    # 2. 对每个 bot 拉群列表并写入
    total_inserted = 0
    for bot in bots:
        bot_name = bot["bot_name"]
        try:
            token = await get_tenant_token(bot["app_id"], bot["app_secret"])
            chats = await list_bot_chats(token)
            logger.info("[%s] Found %d group chats", bot_name, len(chats))

            if not chats:
                continue

            async with AsyncSessionLocal() as session:
                for chat in chats:
                    await session.execute(
                        text(
                            "INSERT INTO bot_chat_presence (chat_id, bot_name) "
                            "VALUES (:cid, :bn) "
                            "ON CONFLICT (chat_id, bot_name) DO UPDATE "
                            "SET is_active = true, updated_at = now()"
                        ),
                        {"cid": chat["chat_id"], "bn": bot_name},
                    )
                await session.commit()
                total_inserted += len(chats)
                logger.info("[%s] Upserted %d records", bot_name, len(chats))

        except Exception as e:
            logger.error("[%s] Failed: %s", bot_name, e, exc_info=True)

    logger.info("Migration done. Total records: %d", total_inserted)


if __name__ == "__main__":
    asyncio.run(main())
