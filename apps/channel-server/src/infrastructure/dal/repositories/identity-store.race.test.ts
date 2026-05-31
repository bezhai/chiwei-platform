import { describe, it, expect, mock, beforeEach } from 'bun:test';

// 钉死 TypeOrmIdentityStore.upsertMapping 的 SQL 形态与 race 安全语义。
//
// 真实 prod bug（已在丢消息）：
//   旧实现 SQL =
//     WITH ins AS (
//       INSERT ... ON CONFLICT ON CONSTRAINT uq_* DO NOTHING
//       RETURNING <internal_col> AS internal_id
//     )
//     SELECT internal_id FROM ins
//     UNION ALL
//     SELECT <internal_col> AS internal_id FROM <table>
//     WHERE channel=$2 AND <channel_id_col>=$3
//     LIMIT 1
//   并发同 (channel, channel_id) 第二次进来：INSERT 走 ON CONFLICT DO NOTHING
//   → ins 0 行；PG read committed 下同一 CTE 的 UNION ALL SELECT 看不到那条
//   conflicting row（snapshot 边界） → SELECT 0 行 → 整体 0 行 → store 抛
//   "returned no internal id; store is inconsistent" → fail-loud 链路中断 →
//   prod 丢消息。
//
// 修复方向：改成 ON CONFLICT ... DO UPDATE SET <indexed_col>=EXCLUDED.<...>
// RETURNING <internal_col>。DO UPDATE 即使 SET no-op 也"涉及行"，永远把那行
// 通过 RETURNING 拉回，不再依赖 CTE UNION ALL 兜底。三张表 (identity_user_v2 /
// identity_conversation_v2 / identity_message_v2) 同款 SQL 全部统一。
//
// 不连真实 PG —— mock @ormconfig 让我们能捕获实际下发的 SQL 文本，并按需
// 模拟 query 返回行。

let capturedSqls: string[] = [];
let nextRows: Array<Array<{ internal_id: string }>> = [];

const queryMock = mock(async (sql: string, _params: unknown[]) => {
    capturedSqls.push(sql);
    // 顺序消费下一个预设的返回值；没预设则返回单行（默认 happy path）。
    const next = nextRows.shift();
    if (next !== undefined) return next;
    return [{ internal_id: 'DEFAULT_ULID_______________' }];
});

mock.module('@ormconfig', () => ({
    default: {
        query: queryMock,
    },
}));

// 让 identity-store.ts 顶层 import 的 entities 不实际走 TypeORM 解析（解析
// 流程在测试环境下没意义；TypeOrmIdentityStore 真正用到 KIND_TABLE.entity
// 的只在 findInternalId/findChannelRef，本测试只覆盖 upsertMapping 路径）。
mock.module('@entities/identity-mapping', () => ({
    IdentityUser: class {},
    IdentityConversation: class {},
    IdentityMessage: class {},
}));

const { TypeOrmIdentityStore } = await import('./identity-store');

const KIND_CASES = [
    {
        kind: 'user' as const,
        table: 'identity_user_v2',
        internalCol: 'internal_user_id',
        channelIdCol: 'channel_user_id',
        constraint: 'uq_identity_user_v2_channel',
    },
    {
        kind: 'chat' as const,
        table: 'identity_conversation_v2',
        internalCol: 'internal_conversation_id',
        channelIdCol: 'channel_conversation_id',
        constraint: 'uq_identity_conversation_v2_channel',
    },
    {
        kind: 'message' as const,
        table: 'identity_message_v2',
        internalCol: 'internal_message_id',
        channelIdCol: 'channel_message_id',
        constraint: 'uq_identity_message_v2_channel',
    },
];

describe('TypeOrmIdentityStore.upsertMapping race-safe SQL 形态契约', () => {
    beforeEach(() => {
        capturedSqls = [];
        nextRows = [];
        queryMock.mockClear();
    });

    for (const c of KIND_CASES) {
        it(`${c.kind}: SQL 必须用 DO UPDATE + RETURNING，不得用 DO NOTHING + UNION ALL SELECT 兜底`, async () => {
            // 预设 RETURNING 返回单行（DO UPDATE 永远 RETURNING）
            nextRows.push([{ internal_id: 'ULID_FROM_RETURNING_______' }]);

            const store = new TypeOrmIdentityStore();
            const got = await store.upsertMapping({
                kind: c.kind,
                channel: 'lark',
                channelId: 'om_xxx',
                internalId: 'ULID_FRESH_______________0',
            });
            expect(got).toBe('ULID_FROM_RETURNING_______');

            expect(capturedSqls).toHaveLength(1);
            const sql = capturedSqls[0]!;

            // 必须含目标表/约束/列名
            expect(sql).toContain(c.table);
            expect(sql).toContain(c.constraint);
            expect(sql).toContain(c.internalCol);
            expect(sql).toContain(c.channelIdCol);

            // 必须是 DO UPDATE，不再 DO NOTHING
            expect(sql).toMatch(/ON\s+CONFLICT\s+ON\s+CONSTRAINT\s+\w+\s+DO\s+UPDATE/i);
            expect(sql).not.toMatch(/DO\s+NOTHING/i);

            // 不应再有 CTE/UNION ALL 兜底（DO UPDATE 永远 RETURNING）
            expect(sql).not.toMatch(/UNION\s+ALL/i);
            expect(sql).not.toMatch(/\bWITH\s+ins\b/i);

            // 必须 RETURNING internalCol
            expect(sql).toMatch(
                new RegExp(`RETURNING\\s+${c.internalCol}`, 'i'),
            );

            // EXCLUDED.* 引用（DO UPDATE SET 用到 EXCLUDED 才是规范写法）
            expect(sql).toMatch(/EXCLUDED\./);
        });
    }

    it('race 复现：旧 SQL pattern 下 conflict 路径若返回 0 行会 fail-loud；新 SQL DO UPDATE 永远返回单行，store 不再抛 inconsistent', async () => {
        // 这条直接对着 race 行为：模拟"DB 在 conflict 路径仍然 RETURNING 一行"
        // —— 这是 DO UPDATE 的承诺，PG 引擎保证。
        // 旧实现下当 conflict 触发 DO NOTHING 时 query 返回 []，store 抛
        // inconsistent。新实现下 DB 永远不会返回 []，所以 store 永远拿到 id。
        nextRows.push([{ internal_id: 'EXISTING_ULID_______________' }]);

        const store = new TypeOrmIdentityStore();
        const id = await store.upsertMapping({
            kind: 'message',
            channel: 'lark',
            channelId: 'om_existing',
            internalId: 'CANDIDATE_ULID_NOT_USED____',
        });

        expect(id).toBe('EXISTING_ULID_______________');
        // 不抛 "returned no internal id" / "store is inconsistent"
        // —— 通过 await 不抛 就证明了。
    });
});
