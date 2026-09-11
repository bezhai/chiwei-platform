// LarkOutboundTables 的真身。出站这一侧唯一知道 TypeORM 和 SQL 的地方。
//
// 每个方法一条语句，不做判断 —— 该不该写、写哪个 id、撞了怎么办，全在 deliver.ts
// 里决定。这条界线让整条出站链能在一台连不到数据库的机器上完整地测，也让"发出去
// 的 SQL 长什么样"能被单独钉住（见 postgres-tables.test.ts）。

import type { DataSource, EntityManager } from 'typeorm';
import { CommonMessage } from '@inner/shared/entities';

import { LarkBaseChatInfo } from '../../entities/lark-base-chat-info';
import { LarkMessage } from '../../entities/lark-message';
import type {
    LarkAssistantMessageRow,
    LarkOutboundMapping,
    LarkOutboundStore,
    LarkOutboundTables,
    LarkProactiveMessageRow,
    LarkRecallTables,
} from './tables';

function tablesOn(manager: EntityManager): LarkOutboundTables {
    return {
        async chatIdOf(commonConversationId): Promise<string | null> {
            const row = await manager.getRepository(LarkBaseChatInfo).findOne({
                where: { common_conversation_id: commonConversationId },
            });
            return row?.chat_id ?? null;
        },

        async omIdOf(commonMessageId): Promise<string | null> {
            const row = await manager.getRepository(LarkMessage).findOne({
                where: { common_message_id: commonMessageId },
            });
            return row?.om_id ?? null;
        },

        async commonMessageIdOf(omId): Promise<string | null> {
            const row = await manager.getRepository(LarkMessage).findOne({
                where: { om_id: omId },
            });
            return row?.common_message_id ?? null;
        },

        async insertCommonMessage(row: LarkAssistantMessageRow): Promise<void> {
            await manager
                .createQueryBuilder()
                .insert()
                .into(CommonMessage)
                .values({
                    common_message_id: row.common_message_id,
                    channel: row.channel,
                    common_conversation_id: row.common_conversation_id,
                    common_user_id: row.common_user_id,
                    sender_display_name: row.sender_display_name,
                    role: row.role,
                    content: row.content as never,
                    content_text: row.content_text,
                    common_root_message_id: row.common_root_message_id,
                    common_reply_message_id: row.common_reply_message_id,
                    scope: row.scope,
                    message_type: row.message_type,
                    bot_name: row.bot_name,
                    event_time: row.event_time,
                    response_id: row.response_id,
                    agent_outbound_id: row.agent_outbound_id,
                })
                // 重投同一段回复时静默 no-op。
                .orIgnore()
                .execute();
        },

        async insertLarkMessage(row: LarkOutboundMapping): Promise<void> {
            // **跟入站那条不一样：这一条也 or-ignore。** 理由见 tables.ts 的端口注释
            // ——出站的 om_id 会撞（平台没返回 message_id 时落的是合成键），而撞了
            // 回滚会把整条回复的落库全丢，消息却已经真的发出去了。
            await manager
                .createQueryBuilder()
                .insert()
                .into(LarkMessage)
                .values({
                    om_id: row.om_id,
                    common_message_id: row.common_message_id,
                    chat_id: row.chat_id,
                    message_type: row.message_type,
                })
                .orIgnore()
                .execute();
        },
    };
}

/**
 * 撤回主动消息那两条语句。
 *
 * 不放进 tablesOn：那一组是**发消息**要的语句，整组会被 atomically 换到事务连接上
 * 跑；撤回这两条既不在那个事务里，也不该跟着它一起被换。
 */
function recallTablesOn(manager: EntityManager): Omit<LarkRecallTables, 'omIdOf'> {
    return {
        async messagesOfAgentOutbound(agentOutboundId): Promise<LarkProactiveMessageRow[]> {
            const rows = await manager.getRepository(CommonMessage).find({
                // 撤回只要"哪几行、谁发的、撤过没有"。整行取回来会把 content 那个
                // jsonb 一起拖出来，而它对撤回一点用都没有。
                select: {
                    common_message_id: true,
                    bot_name: true,
                    recalled_at: true,
                },
                // 参数就是标准 uuid 文本，列侧不套任何函数 —— 套了（比如 CAST 成
                // text）就绕开 agent_outbound_id 上那个索引走全表扫。
                where: { agent_outbound_id: agentOutboundId },
                // 一次开口的多段按发送先后撤。同一毫秒落下的两段靠主键兜底，
                // 否则顺序由 PG 决定，看上去随机。
                order: { event_time: 'ASC', common_message_id: 'ASC' },
            });
            return rows.map((row) => ({
                common_message_id: row.common_message_id,
                bot_name: row.bot_name ?? null,
                // 这一列可空，撤回那一侧要按"非空 = 已经撤过"判短路，所以 undefined
                // 归一成 null —— 两种写法在那里是同一件事，但只有一种能被断言。
                recalled_at: row.recalled_at ?? null,
            }));
        },

        async markRecalled(commonMessageId, recalledAt): Promise<void> {
            // 只碰 recalled_at 这一列：这张表三个服务共写，多写一列就是覆盖别人写的
            // 结论。
            await manager
                .createQueryBuilder()
                .update(CommonMessage)
                .set({ recalled_at: recalledAt })
                .where('common_message_id = :commonMessageId', { commonMessageId })
                .execute();
        },
    };
}

export function postgresLarkOutboundTables(
    dataSource: DataSource,
): LarkOutboundStore & LarkRecallTables {
    return {
        ...tablesOn(dataSource.manager),
        ...recallTablesOn(dataSource.manager),
        atomically: (run) => dataSource.transaction((manager) => run(tablesOn(manager))),
    };
}
