// LarkResponseLedger 的真身。三条语句，各一件事。
//
// 这张表三方共写而且没有 channel 列，所以每条语句都要**只碰自己该碰的列**：
// safety_status / safety_result 归安全判定与撤回链路，persona_id 归 agent-service，
// 出站只碰 replies / response_text / status 这三列。多覆盖一列，DB 层拦不住。

import type { DataSource } from 'typeorm';
import { CommonAgentResponse } from '@inner/shared/entities';

import type {
    LarkAgentResponseRow,
    LarkResponseLedger,
    LarkResponseOutcome,
    LarkSafetyOutcome,
} from './ledger';

export function postgresLarkResponseLedger(dataSource: DataSource): LarkResponseLedger {
    const repo = () => dataSource.getRepository(CommonAgentResponse);

    return {
        async find(sessionId): Promise<LarkAgentResponseRow | null> {
            const row = await repo().findOneBy({ session_id: sessionId });
            return row
                ? {
                      session_id: row.session_id,
                      bot_name: row.bot_name,
                      replies: row.replies,
                      safety_status: row.safety_status,
                  }
                : null;
        },

        async appendReply(sessionId, reply): Promise<void> {
            // jsonb 的 `||` 在**两个数组**之间才是追加，所以拼进去的是 `[reply]`
            // 而不是 `reply`——拼一个对象进去会变成键合并，整个 replies 被压成一个
            // 对象，读的人看到的是"只回了一段"。
            await repo()
                .createQueryBuilder()
                .update(CommonAgentResponse)
                .set({ replies: () => `COALESCE(replies, '[]'::jsonb) || :replyEntry::jsonb` })
                .setParameter('replyEntry', JSON.stringify([reply]))
                .where('session_id = :sid', { sid: sessionId })
                .execute();
        },

        async settle(sessionId, outcome: LarkResponseOutcome): Promise<void> {
            // responseText 缺省 = **不把这一列放进 SET**。放进去写 undefined，
            // TypeORM 的行为取决于版本，而猜错的后果是把前面几段落好的全文抹成空。
            await repo().update(
                { session_id: sessionId },
                {
                    status: outcome.status,
                    ...(outcome.responseText === undefined
                        ? {}
                        : { response_text: outcome.responseText }),
                },
            );
        },

        async settleSafety(sessionId, outcome: LarkSafetyOutcome): Promise<void> {
            // 只有 safety 这两列进 SET。replies 归出站、status / response_text 归出站
            // 收尾、persona_id 归 agent-service —— 这条语句多带一列，DB 层拦不住。
            await repo().update(
                { session_id: sessionId },
                {
                    safety_status: outcome.status,
                    safety_result: {
                        reason: outcome.reason,
                        detail: outcome.detail,
                        recalled: outcome.recalled,
                        failed: outcome.failed,
                        // 物理列名口径：agent-service 读的就是这个 key。
                        checked_at: outcome.checkedAt,
                    },
                },
            );
        },
    };
}
