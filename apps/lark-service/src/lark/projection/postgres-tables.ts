// LarkTables 的真身。**本服务里唯一知道 TypeORM 和 SQL 的地方。**
//
// 每个方法是一条语句，不做任何判断 —— 该不该写、写哪个 id，全在 inbound-projection.ts
// 里决定。这条界线让投影的逻辑能在没有数据库的机器上完整地测，也让"发出去的 SQL
// 长什么样"能被单独钉住（见 postgres-tables.test.ts）。
//
// 实体的属性名是驼峰、端口的字段名是列名，中间的翻译只在这一层做一次。

import type { DataSource, EntityManager } from 'typeorm';
import {
    CommonBotPresence,
    CommonConversation,
    CommonMessage,
    CommonUser,
} from '@inner/shared/entities';

import { LarkBaseChatInfo } from '../../entities/lark-base-chat-info';
import { LarkGroupChatInfo } from '../../entities/lark-group-chat-info';
import { LarkGroupMember } from '../../entities/lark-group-member';
import { LarkMessage } from '../../entities/lark-message';
import { LarkUser } from '../../entities/lark-user';
import { LarkUserOpenId } from '../../entities/lark-user-open-id';
import { UserGroupBinding } from '../../entities/user-group-binding';
import type {
    CommonConversationRow,
    CommonMessageRow,
    CommonUserRow,
    LarkChatRow,
    LarkGroupBinding,
    LarkGroupChatFacts,
    LarkGroupMemberRow,
    LarkMessageRow,
    LarkStore,
    LarkTables,
    LarkUserLink,
    LarkUserProfile,
} from './tables';

function tablesOn(manager: EntityManager): LarkTables {
    return {
        async larkUserByOpenId(appId, openId): Promise<LarkUserLink | null> {
            const row = await manager.getRepository(LarkUserOpenId).findOne({
                where: { appId, openId },
            });
            return row ? larkUserLinkOf(row) : null;
        },

        async larkUserByUnionId(unionId): Promise<LarkUserLink | null> {
            // 排序不是可有可无：同一个 union_id 在多个飞书应用下各有一行，取哪一条
            // 要是不确定，两个进程会各选一条、把同一个人分成两半。
            const row = await manager.getRepository(LarkUserOpenId).findOne({
                where: { unionId },
                order: { commonUserId: 'ASC' },
            });
            return row ? larkUserLinkOf(row) : null;
        },

        async larkUserProfile(unionId): Promise<LarkUserProfile | null> {
            const row = await manager.getRepository(LarkUser).findOne({
                where: { union_id: unionId },
            });
            return row
                ? {
                      name: row.name,
                      avatar_origin: row.avatar_origin,
                      is_admin: row.is_admin,
                  }
                : null;
        },

        async larkChat(chatId): Promise<LarkChatRow | null> {
            const row = await manager.getRepository(LarkBaseChatInfo).findOne({
                where: { chat_id: chatId },
            });
            return row
                ? {
                      chat_id: row.chat_id,
                      chat_mode: row.chat_mode,
                      common_conversation_id: row.common_conversation_id,
                      // gray_config 就在同一行上，**刻意不带出去**（见 tables.ts 的
                      // LarkChatPermission）。
                      permission_config: row.permission_config,
                  }
                : null;
        },

        async larkGroupMember(chatId, unionId): Promise<LarkGroupMemberRow | null> {
            const row = await manager.getRepository(LarkGroupMember).findOne({
                where: { chat_id: chatId, union_id: unionId },
            });
            return row
                ? {
                      chat_id: row.chat_id,
                      union_id: row.union_id,
                      // 退群的人不从表里删，只打这一位。判断留给调用方。
                      is_leave: row.is_leave,
                      is_manager: row.is_manager,
                      is_owner: row.is_owner,
                  }
                : null;
        },

        async larkGroupBinding(chatId, unionId): Promise<LarkGroupBinding | null> {
            const row = await manager.getRepository(UserGroupBinding).findOne({
                where: { chatId, userUnionId: unionId },
            });
            return row
                ? {
                      user_union_id: row.userUnionId,
                      chat_id: row.chatId,
                      // 解绑是软删，这一位就是"还算不算数"。
                      is_active: row.isActive,
                  }
                : null;
        },

        async larkGroupChat(chatId): Promise<LarkGroupChatFacts | null> {
            const row = await manager.getRepository(LarkGroupChatInfo).findOne({
                where: { chat_id: chatId },
            });
            return row
                ? {
                      name: row.name,
                      avatar: row.avatar,
                      user_count: row.user_count,
                      is_leave: row.is_leave,
                      download_has_permission_setting: row.download_has_permission_setting,
                  }
                : null;
        },

        async larkMessage(omId): Promise<LarkMessageRow | null> {
            const row = await manager.getRepository(LarkMessage).findOne({
                where: { om_id: omId },
            });
            return row
                ? {
                      om_id: row.om_id,
                      common_message_id: row.common_message_id,
                      chat_id: row.chat_id,
                      sender_open_id: row.sender_open_id,
                      sender_union_id: row.sender_union_id,
                      root_om_id: row.root_om_id,
                      reply_om_id: row.reply_om_id,
                      message_type: row.message_type,
                      raw_event: row.raw_event,
                  }
                : null;
        },

        async claimCommonUserId(key, facts, candidate): Promise<string> {
            // COALESCE 是整条语句的重点：已经写进去的 common_user_id **不让**，
            // EXCLUDED（也就是我们的 candidate）只在原来是空的时候才生效。这一条
            // 语句就是"自然键首写者成为 canonical"，两个进程同时跑也只有一个赢。
            //
            // union_id / name 的 COALESCE 方向**反过来**：新值优先，新值为空才留旧
            // 值。因为认领是"读在前、写在后"—— 调用方先读这一行、读不到就把空值传
            // 进来；并发的另一条流可能刚把有效的 union_id 写进去，而本流那次读还是
            // 空的。无条件 `= EXCLUDED` 会把对方刚写的抹掉，union_id 抹掉就等于把
            // 跨飞书应用收敛身份的唯一依据抹掉。空串和 NULL 都算"没有值"，NULLIF
            // 负责把前者变成后者。
            //
            // 用 onConflict 而不是 orUpdate：TypeORM 的 orUpdate 只会写
            // `= EXCLUDED.x`，表达不了 COALESCE。INSERT 那半截仍由实体元数据生成，
            // 手写的只有冲突子句。
            const inserted = await manager
                .createQueryBuilder()
                .insert()
                .into(LarkUserOpenId)
                .values({
                    appId: key.app_id,
                    openId: key.open_id,
                    unionId: facts.union_id,
                    name: facts.name,
                    commonUserId: candidate,
                })
                .onConflict(
                    '("app_id", "open_id") DO UPDATE SET ' +
                        '"union_id" = COALESCE(NULLIF(EXCLUDED."union_id", \'\'), ' +
                        '"lark_user_open_id"."union_id"), ' +
                        '"name" = COALESCE(NULLIF(EXCLUDED."name", \'\'), ' +
                        '"lark_user_open_id"."name"), ' +
                        '"common_user_id" = COALESCE(' +
                        '"lark_user_open_id"."common_user_id", EXCLUDED."common_user_id")',
                )
                .returning('common_user_id')
                .execute();
            return (inserted.raw as Array<{ common_user_id: string }>)[0]!.common_user_id;
        },

        async linkLarkUser(key, commonUserId): Promise<void> {
            await manager
                .getRepository(LarkUserOpenId)
                .update({ appId: key.app_id, openId: key.open_id }, { commonUserId });
        },

        async claimCommonConversationId(chat, candidate): Promise<string> {
            // chat_mode 只在建行时写：别的代码路径（"用户进入私聊"事件）会先建一条
            // 只有 chat_id / chat_mode 的行，它知道的比我们从 scope 推的更准。
            const inserted = await manager
                .createQueryBuilder()
                .insert()
                .into(LarkBaseChatInfo)
                .values({
                    chat_id: chat.chat_id,
                    chat_mode: chat.chat_mode,
                    common_conversation_id: candidate,
                })
                .onConflict(
                    '("chat_id") DO UPDATE SET "common_conversation_id" = COALESCE(' +
                        '"lark_base_chat_info"."common_conversation_id", ' +
                        'EXCLUDED."common_conversation_id")',
                )
                .returning('common_conversation_id')
                .execute();
            return (inserted.raw as Array<{ common_conversation_id: string }>)[0]!
                .common_conversation_id;
        },

        async saveCommonUser(row: CommonUserRow): Promise<void> {
            // upsert 只覆盖"值不是 undefined"的列：档案里查不到名字的时候，不该把
            // 库里已有的名字抹成空。
            await manager.getRepository(CommonUser).upsert(
                {
                    common_user_id: row.common_user_id,
                    channel: row.channel,
                    display_name: row.display_name,
                },
                ['common_user_id'],
            );
        },

        async saveCommonConversation(row: CommonConversationRow): Promise<void> {
            await manager.getRepository(CommonConversation).upsert(
                {
                    common_conversation_id: row.common_conversation_id,
                    channel: row.channel,
                    scope: row.scope,
                    display_name: row.display_name,
                    avatar_url: row.avatar_url,
                    member_count: row.member_count,
                    is_active: row.is_active,
                    attachment_policy: row.attachment_policy,
                },
                ['common_conversation_id'],
            );
        },

        async insertCommonMessage(row: CommonMessageRow): Promise<void> {
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
                    mentioned_common_user_ids: row.mentioned_common_user_ids,
                    scope: row.scope,
                    message_type: row.message_type,
                    bot_name: row.bot_name,
                    event_time: row.event_time,
                })
                // 重放的地基：同一条消息第二次进来时静默 no-op。
                .orIgnore()
                .execute();
        },

        async insertLarkMessage(row: LarkMessageRow): Promise<void> {
            // 这条**不**加 orIgnore：撞主键说明有人并发写了别的映射，必须让整个事务
            // 回滚，而不是留下一条只有 common_message 的孤儿记录。
            await manager
                .createQueryBuilder()
                .insert()
                .into(LarkMessage)
                .values({
                    om_id: row.om_id,
                    common_message_id: row.common_message_id,
                    chat_id: row.chat_id,
                    sender_open_id: row.sender_open_id,
                    sender_union_id: row.sender_union_id,
                    root_om_id: row.root_om_id,
                    reply_om_id: row.reply_om_id,
                    message_type: row.message_type,
                    // 整包 JSONB。TypeORM 的 partial-entity 类型描述不了任意形状的
                    // 对象列，这里的 cast 只是绕过它，落库的仍是原样的报文。
                    raw_event: row.raw_event as never,
                })
                .execute();
        },

        async claimCommonMessageForBot(claim): Promise<void> {
            // role='user' 是护栏：一条消息在 common_message 里还会有 assistant 那一行，
            // 认领说的只是"这条**用户消息**由谁处理"。
            const result = await manager.getRepository(CommonMessage).update(
                { common_message_id: claim.common_message_id, role: 'user' },
                { bot_name: claim.bot_name, common_user_id: claim.common_user_id },
            );
            if (!result.affected) {
                throw new Error(
                    `no user message ${claim.common_message_id} to claim for ` +
                        `bot ${claim.bot_name}; it was never written to common_message`,
                );
            }
        },

        async insertLarkGroupBinding(chatId, unionId): Promise<void> {
            // 普通 insert，**不加 onConflict**：(user_union_id, chat_id) 上没有唯一
            // 约束，PG 会直接拒绝一个指不到索引的冲突子句。判重靠调用方先读一次。
            await manager
                .createQueryBuilder()
                .insert()
                .into(UserGroupBinding)
                .values({ userUnionId: unionId, chatId, isActive: true })
                .execute();
        },

        async setLarkGroupBindingActive(chatId, unionId, isActive): Promise<void> {
            await manager
                .getRepository(UserGroupBinding)
                .update({ userUnionId: unionId, chatId }, { isActive });
        },

        async setLarkChatPermission(chatId, patch): Promise<void> {
            // `jsonb ||` 是合并：同一列上的其他开关原样留着。整列覆写会把它们一起抹掉。
            //
            // COALESCE 不能省 —— 这一列 nullable，而 PG 里 `NULL || anything` 还是
            // NULL。少了它，从来没配过开关的老会话第一次开复读会写进去一个 NULL，
            // 语句成功、什么也没存下。
            //
            // patch 走绑定参数（`::jsonb` 的显式转型是给 PG 定类型用的，未定类型的
            // 参数在 `jsonb || ?` 这个位置上解析不出来）。
            await manager
                .createQueryBuilder()
                .update(LarkBaseChatInfo)
                .set({
                    permission_config: () =>
                        `COALESCE("permission_config", '{}'::jsonb) || :patch::jsonb`,
                })
                .setParameter('patch', JSON.stringify(patch))
                .where('chat_id = :chatId', { chatId })
                .execute();
        },

        async markBotPresent(commonConversationId, botName, isActive): Promise<void> {
            await manager.getRepository(CommonBotPresence).upsert(
                {
                    common_conversation_id: commonConversationId,
                    bot_name: botName,
                    is_active: isActive,
                    updated_at: new Date(),
                },
                ['common_conversation_id', 'bot_name'],
            );
        },
    };
}

function larkUserLinkOf(row: LarkUserOpenId): LarkUserLink {
    return {
        app_id: row.appId,
        open_id: row.openId,
        union_id: row.unionId,
        name: row.name,
        common_user_id: row.commonUserId,
    };
}

export function postgresLarkTables(dataSource: DataSource): LarkStore {
    return {
        ...tablesOn(dataSource.manager),
        atomically: (run) => dataSource.transaction((manager) => run(tablesOn(manager))),
    };
}
