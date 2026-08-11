// 台账真身发出去的 SQL。
//
// 这张表是三方共写的（飞书出站在本服务、QQ 出站在 channel-server、人设与安全判定在
// agent-service），而它没有 channel 列。所以每一条语句都得看清楚：写错列、写成
// 读-改-写、或者把不该碰的列一起覆盖掉，DB 层都拦不住。

import { beforeEach, describe, expect, it } from 'bun:test';

import { LARK_SERVICE_ENTITIES } from '../../ormconfig';
import { recordingDataSource, type RecordedStatement } from '../recording-data-source';
import type { LarkResponseLedger } from './ledger';
import { postgresLarkResponseLedger } from './postgres-ledger';

interface Harness {
    ledger: LarkResponseLedger;
    recorded: RecordedStatement[];
    reply(rows: Array<Record<string, unknown>>): void;
    sqlOf(fragment: string): RecordedStatement;
}

function harness(): Harness {
    const recorder = recordingDataSource([...LARK_SERVICE_ENTITIES]);
    return { ...recorder, ledger: postgresLarkResponseLedger(recorder.dataSource) };
}

let h: Harness;
beforeEach(() => {
    h = harness();
});

describe('读台账', () => {
    it('按 session_id 查，取出站与撤回各自要的那几列', async () => {
        h.reply([
            {
                CommonAgentResponse_session_id: 'sess-1',
                CommonAgentResponse_bot_name: 'chiwei',
                CommonAgentResponse_replies: [{ common_message_id: 'cm_1', sent_at: 'ts' }],
                CommonAgentResponse_safety_status: 'pending',
                CommonAgentResponse_response_id: 'r-1',
            },
        ]);

        expect(await h.ledger.find('sess-1')).toEqual({
            session_id: 'sess-1',
            bot_name: 'chiwei',
            replies: [{ common_message_id: 'cm_1', sent_at: 'ts' }],
            safety_status: 'pending',
        });

        const select = h.sqlOf('FROM "common_agent_response"');
        expect(select.sql).toContain('"session_id" = $1');
        expect(select.params).toEqual(['sess-1']);
    });

    it('查不到就是 null', async () => {
        h.reply([]);
        expect(await h.ledger.find('sess-missing')).toBeNull();
    });
});

describe('落安全终态', () => {
    // 写入矩阵里 safety_status / safety_result 是 recall worker 和 agent-service 双向
    // 写的那一对，而这张表没有 channel 列 —— DB 层拦不住越界。所以这条语句碰了哪些列
    // 必须被直接钉住，不能只验"业务算出来的状态对不对"。
    const outcome = {
        status: 'recalled' as const,
        reason: 'unsafe',
        detail: '违规内容',
        recalled: 2,
        failed: 1,
        checkedAt: '2026-08-11T00:00:00.000Z',
    };

    it('safety_status 与 safety_result 一起写，两列都在 SET 里', async () => {
        await h.ledger.settleSafety('sess-1', outcome);

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.sql).toContain('"safety_status"');
        expect(update.sql).toContain('"safety_result"');
        expect(update.sql).toContain('WHERE "session_id" = $');
        expect(update.params).toContain('recalled');
        expect(update.params).toContain('sess-1');
    });

    it('safety_result 的字段名用物理列名口径（checked_at 不是 checkedAt）', async () => {
        // agent-service 读的是这个 jsonb 里的 key，写错一个键就是它读不到。
        await h.ledger.settleSafety('sess-1', outcome);

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.params).toContain(
            JSON.stringify({
                reason: 'unsafe',
                detail: '违规内容',
                recalled: 2,
                failed: 1,
                checked_at: '2026-08-11T00:00:00.000Z',
            }),
        );
    });

    it('只碰这两列 —— replies / status / response_text / persona_id 归别人写', async () => {
        await h.ledger.settleSafety('sess-1', outcome);

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.sql).not.toContain('"replies"');
        expect(update.sql).not.toContain('"status"');
        expect(update.sql).not.toContain('"response_text"');
        expect(update.sql).not.toContain('"persona_id"');
    });

    it('撤回失败的终态同样落两列', async () => {
        await h.ledger.settleSafety('sess-1', {
            status: 'recall_failed',
            reason: 'unsafe',
            recalled: 0,
            failed: 0,
            checkedAt: '2026-08-11T00:00:00.000Z',
        });

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.params).toContain('recall_failed');
        // detail 缺省时这个 key 直接不出现在 jsonb 里（JSON.stringify 丢掉 undefined），
        // 与拆分前 channel-server 写出来的形状逐字一致。
        expect(update.params).toContain(
            JSON.stringify({
                reason: 'unsafe',
                recalled: 0,
                failed: 0,
                checked_at: '2026-08-11T00:00:00.000Z',
            }),
        );
    });
});

describe('追加 replies', () => {
    it('走 jsonb 的 `||` 拼接，不是读-改-写', async () => {
        await h.ledger.appendReply('sess-1', {
            common_message_id: 'cm_1',
            content_type: 'post',
            sent_at: '2026-08-10T00:00:00.000Z',
        });

        const update = h.sqlOf('UPDATE "common_agent_response"');
        // 同一次回答的多段是并发消费的：读-改-写会让后写的那一段挤掉前一段。
        expect(update.sql).toContain('||');
        expect(update.sql).toContain(`COALESCE("replies", '[]'::jsonb)`);
        expect(update.sql).toContain('::jsonb');
        expect(update.sql).toContain('WHERE "session_id" = $');
        // 拼进去的是一个**数组**：jsonb 的 || 拼两个数组才是追加，拼一个对象是合并。
        expect(update.params).toContain(
            JSON.stringify([
                {
                    common_message_id: 'cm_1',
                    content_type: 'post',
                    sent_at: '2026-08-10T00:00:00.000Z',
                },
            ]),
        );
        expect(update.params).toContain('sess-1');
    });

    it('只碰 replies 一列', async () => {
        await h.ledger.appendReply('sess-1', {
            common_message_id: 'cm_1',
            sent_at: '2026-08-10T00:00:00.000Z',
        });

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.sql).not.toContain('"status"');
        expect(update.sql).not.toContain('"response_text"');
        // safety_* 归 agent-service 和撤回链路写，这条语句碰它就是越界。
        expect(update.sql).not.toContain('safety_status');
    });
});

describe('落终态', () => {
    it('带全文时同时写 response_text 和 status', async () => {
        await h.ledger.settle('sess-1', { status: 'completed', responseText: '全文' });

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.sql).toContain('"response_text"');
        expect(update.sql).toContain('"status"');
        expect(update.params).toContain('全文');
        expect(update.params).toContain('completed');
    });

    it('不带全文时**不碰** response_text —— 写空会抹掉前面几段落好的正文', async () => {
        await h.ledger.settle('sess-1', { status: 'completed' });

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.sql).not.toContain('"response_text"');
        expect(update.params).toContain('completed');
    });

    it('失败终态只写 status', async () => {
        await h.ledger.settle('sess-1', { status: 'failed' });

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.sql).not.toContain('"response_text"');
        expect(update.params).toContain('failed');
        expect(update.params).toContain('sess-1');
    });

    it('终态一律不碰 safety_* —— 那两列归安全判定和撤回链路', async () => {
        await h.ledger.settle('sess-1', { status: 'completed', responseText: '全文' });

        const update = h.sqlOf('UPDATE "common_agent_response"');
        expect(update.sql).not.toContain('safety_status');
        expect(update.sql).not.toContain('safety_result');
    });
});
