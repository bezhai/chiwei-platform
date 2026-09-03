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

export function postgresLarkOutboundTables(dataSource: DataSource): LarkOutboundStore {
    return {
        ...tablesOn(dataSource.manager),
        atomically: (run) => dataSource.transaction((manager) => run(tablesOn(manager))),
    };
}
