// 斜杠子指令要读写、而现有端口没有覆盖的两张表。
//
//     user_blacklist    `/block` `/unblock` `/blocklist`
//     common_message    `/session`（先按 om_id 走投影端口拿 common_message_id，再读这里）
//
// ## 为什么它们不进 projection/tables.ts
//
// 那个端口描述的是"**一条飞书消息进来**要读写哪些行"，每个方法都能在写入矩阵里找到
// 对应的一行。这两张表的动作不在那条链上：拉黑是管理员的一次性动作，`/session` 读的是
// **另一条**（很久以前的）消息。塞进去会让那个端口不再是"入站链"这个意思。
//
// 所以走 `LarkCommandDeps.database` 那条路 —— 那一项存在的理由就是这个（"还没有专门
// 端口的表从这里自建仓储"）。仓储在这里，端口也在这里，指令层只认端口。
//
// ## user_blacklist 是共享表，不是飞书私有的
//
// 共享规则引擎的 `NotBlocked` 也读它。**而且两边的值口径不一样**：那条规则按
// `common_user_id` 查（列名 union_id 是历史遗留），这三条子指令写的是**飞书的
// union_id**。所以今天 `/block` 拉黑的人，`NotBlocked` 拦不住 —— 与拆分前逐字一致，
// 登记在案，不在这一批修（修它要决定"黑名单到底按哪个 id 体系"，是跨渠道的口径决定）。

import type { DataSource } from 'typeorm';
import { CommonMessage, UserBlacklist } from '@inner/shared/entities';

// ---------------------------------------------------------------------------
// 黑名单
// ---------------------------------------------------------------------------

export interface LarkBlocklist {
    isBlocked(unionId: string): Promise<boolean>;
    /** 记下是谁拉的。重复拉黑由调用方先问 isBlocked 挡住（与拆分前一致）。 */
    block(unionId: string, blockedBy: string): Promise<void>;
    unblock(unionId: string): Promise<void>;
    /** 名单上的所有人。**没有排序** —— 上游那次 find() 也没有。 */
    everyone(): Promise<string[]>;
}

export function postgresBlocklist(database: DataSource): LarkBlocklist {
    // getRepository 逐次取而不是装配期取一次：与本服务其他真身一致，装配期不碰连接。
    const rows = () => database.getRepository(UserBlacklist);

    return {
        async isBlocked(unionId) {
            return (await rows().findOne({ where: { union_id: unionId } })) !== null;
        },
        async block(unionId, blockedBy) {
            await rows().save({ union_id: unionId, blocked_by: blockedBy });
        },
        async unblock(unionId) {
            await rows().delete({ union_id: unionId });
        },
        async everyone() {
            return (await rows().find()).map((row) => row.union_id);
        },
    };
}

// ---------------------------------------------------------------------------
// common_message 上 /session 要看的那两列
// ---------------------------------------------------------------------------

/** 一条公共层消息上 `/session` 关心的两件事。 */
export interface LarkMessageSession {
    /** `assistant` 才是赤尾自己说的。别人的消息没有 session 可查。 */
    role: string;
    /** 挂在哪一次台账上（common_agent_response.session_id）。主动发的没有。 */
    responseId?: string;
}

export interface LarkAgentSessions {
    /** 查不到返回 null。 */
    sessionOf(commonMessageId: string): Promise<LarkMessageSession | null>;
}

export function postgresAgentSessions(database: DataSource): LarkAgentSessions {
    return {
        async sessionOf(commonMessageId) {
            const row = await database.getRepository(CommonMessage).findOne({
                where: { common_message_id: commonMessageId },
                // **role 必须读**：它是"这条是不是赤尾说的"那条判断的唯一依据。
                select: { role: true, response_id: true },
            });
            return row ? { role: row.role, responseId: row.response_id ?? undefined } : null;
        },
    };
}
