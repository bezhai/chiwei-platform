// 撤回请求怎么说清楚要撤哪一条的跨语言线格式契约（消费方这一侧）。
//
// 一条撤回请求恰好用一种定位方式：`session_id`（真人问她、她答的那条链，按会话标识查
// 台账）或者 `outbound_id`（她自己开口那条链，按那次开口的 id 反查公共层那几行）。
// 生产者是 agent-service（Python）的 Recall，两边各写各的字段名和各自的解析，只改一边
// 不会有任何测试变红 —— 撤主动消息的请求会掉进查台账那条路，而台账上一行都没有，于是
// 退避重投三次、进死信，一个飞书接口都不会调，那句话安安静静留在群里，全程零报错。
//
// 所以两侧测试读同一份向量：contracts/recall-locators.json。要骗过测试就得改共享的
// 那一份，而改了共享那一份，两侧一起转红。
//
// 读它的是测试、不是生产代码：两个镜像的 Dockerfile 都不 COPY contracts/，本服务还是
// `bun build --compile` 出来的独立二进制，运行时根本读不到这份文件。跟
// contracts/proactive-message-id.json、contracts/mq-channel-routes.json 是同一套做法。

import { describe, expect, it } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import {
    recallLarkResponse,
    type LarkRecallDeps,
    type LarkRecallPayload,
    type LarkRecallOutcome,
} from './recall';

interface RecallLocatorVector {
    payload: {
        session_id: string | null;
        outbound_id: string | null;
    };
}

interface RecallLocatorsContract {
    by_session: RecallLocatorVector;
    by_outbound: RecallLocatorVector;
}

const CONTRACT_PATH = resolve(import.meta.dir, '../../../../../contracts/recall-locators.json');
const contract = JSON.parse(readFileSync(CONTRACT_PATH, 'utf8')) as RecallLocatorsContract;

/** 向量里必须有值的那一格。空了就是向量缩水，当场炸掉，不让断言退化成比较两个空值。 */
function locator(value: string | null, where: string): string {
    if (!value) throw new Error(`contracts/recall-locators.json has no ${where}`);
    return value;
}

const BY_SESSION = locator(contract.by_session.payload.session_id, 'by_session.payload.session_id');
const BY_OUTBOUND = locator(
    contract.by_outbound.payload.outbound_id,
    'by_outbound.payload.outbound_id',
);

/** 标准 uuid 文本的形状。公共层那一列是 uuid，查它的参数必须长这样。 */
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

interface Traced {
    /** 查过的会话标识。 */
    ledgerLookups: string[];
    /** 按"哪一次开口"反查时传下去的参数，逐字记。 */
    outboundLookups: string[];
    /** 调飞书删掉的那些消息标识。 */
    deleted: string[];
    /** 写了撤回时刻的那些公共层行。 */
    marked: string[];
}

/**
 * 把向量里的 payload 原样喂给真的 recallLarkResponse，看它去查了什么。
 *
 * 两条链的库都种好：分派要是走错，断言"另一条链一次都没被查"立刻转红。
 */
async function run(
    vector: RecallLocatorVector,
): Promise<{ traced: Traced; outcome: LarkRecallOutcome }> {
    const traced: Traced = {
        ledgerLookups: [],
        outboundLookups: [],
        deleted: [],
        marked: [],
    };

    const deps: LarkRecallDeps = {
        ledger: {
            find: async (sessionId) => {
                traced.ledgerLookups.push(sessionId);
                return {
                    session_id: sessionId,
                    bot_name: 'chiwei',
                    replies: [{ common_message_id: 'cm_answer', sent_at: 'ts' }],
                    safety_status: 'pending',
                };
            },
            settleSafety: async () => {},
        },
        store: {
            omIdOf: async (commonMessageId) =>
                commonMessageId === 'cm_answer' ? 'om_answer' : 'om_spoken',
            messagesOfAgentOutbound: async (agentOutboundId) => {
                traced.outboundLookups.push(agentOutboundId);
                return [
                    { common_message_id: 'cm_spoken', bot_name: 'chiwei', recalled_at: null },
                ];
            },
            markRecalled: async (commonMessageId) => {
                traced.marked.push(commonMessageId);
            },
        },
        api: {
            recall: async (messageId) => {
                traced.deleted.push(messageId);
            },
        },
        speakAs: async (_who, say) => say(),
        now: () => 1_700_000_000_000,
    };

    const payload: LarkRecallPayload = {
        channel: 'lark',
        reason: 'unsafe',
        ...vector.payload,
    };
    const outcome = await recallLarkResponse(deps, { payload, retryCount: 0 });
    return { traced, outcome };
}

describe('撤回请求的两种定位方式 — 跨语言契约', () => {
    it('向量本身就说了"恰好用一种"', () => {
        expect(contract.by_session.payload.session_id).toBeTruthy();
        expect(contract.by_session.payload.outbound_id).toBeNull();

        expect(contract.by_outbound.payload.outbound_id).toBeTruthy();
        expect(contract.by_outbound.payload.session_id).toBeNull();
    });

    it('by_session 的那一条：查台账，一次开口都不反查', async () => {
        const { traced, outcome } = await run(contract.by_session);

        expect(traced.ledgerLookups).toEqual([BY_SESSION]);
        expect(traced.outboundLookups).toEqual([]);
        expect(traced.deleted).toEqual(['om_answer']);
        expect(outcome).toMatchObject({ kind: 'settled', status: 'recalled' });
    });

    it('by_outbound 的那一条：按那次开口反查公共层，一行台账都不碰', async () => {
        const { traced, outcome } = await run(contract.by_outbound);

        expect(traced.ledgerLookups).toEqual([]);
        expect(traced.deleted).toEqual(['om_spoken']);
        expect(traced.marked).toEqual(['cm_spoken']);
        expect(outcome).toMatchObject({ kind: 'settled', status: 'recalled' });
    });

    it('线上给的是 hex，查公共层用的是同一串 uuid 的标准写法', async () => {
        // 契约里那一列是 uuid 类型，而线格式上写的是 32 个字符没有短横的 hex。
        // 这条断言不重算一遍转换，只钉两件事：查下去的是标准 uuid 文本，而且去掉短横
        // 之后跟向量里那一串逐字相同。
        const { traced } = await run(contract.by_outbound);

        expect(traced.outboundLookups).toHaveLength(1);
        const queried = traced.outboundLookups[0]!;
        expect(queried).toMatch(UUID_SHAPE);
        expect(queried.replace(/-/g, '')).toBe(BY_OUTBOUND);
    });
});
