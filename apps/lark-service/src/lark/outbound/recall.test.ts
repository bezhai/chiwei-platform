// 把已经发出去的那几条飞书消息删掉。两条链：安全审计判违规之后撤她答的那次会话，
// 以及她自己想收回一次主动开口。
//
// 会话那条的用例是拆分前 channel-server recall-worker 的行为基线：终态短路、逐条撤回
// 的计数语义、台账还没落 replies 时的延时重投、重投到顶之后的终态。主动开口那条不碰
// 台账（主动消息在那张表上一行都没有），撤成功之后写的是 common_message.recalled_at。
//
// 队列那一头（订阅、fail-closed、ACK、重投怎么发出去）在 recall-queue.test.ts。

import { describe, expect, it } from 'bun:test';
import { LarkClient, withLarkErrorCode } from '@inner/lark-utils';
import { context } from '@inner/shared/middleware';
import type { CommonAgentResponseReply } from '@inner/shared/entities';

import { larkSpeakAs } from './bot-context';
import type { LarkSpeakAs } from './deliver';
import type { LarkAgentResponseRow, LarkSafetyOutcome } from './ledger';
import {
    recallLarkResponse,
    type LarkRecallDeps,
    type LarkRecallPayload,
    type LarkRecallRequest,
} from './recall';
import { createSdkLarkApi } from './sdk-lark-api';
import type { LarkProactiveMessageRow, LarkRecallTables } from './tables';

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

/** 种进台账的行：不写的列取默认值。 */
type LedgerSeed = Partial<LarkAgentResponseRow> & { session_id: string };

class MemoryLedger {
    rows = new Map<string, LedgerSeed>();
    safetySettled: Array<{ sessionId: string; outcome: LarkSafetyOutcome }> = [];
    failSettleSafety?: Error;
    /** 查过哪些 session_id。主动开口那条链一次都不该查。 */
    looked: string[] = [];

    seed(seed: LedgerSeed): void {
        this.rows.set(seed.session_id, seed);
    }

    find = async (sessionId: string): Promise<LarkAgentResponseRow | null> => {
        this.looked.push(sessionId);
        const row = this.rows.get(sessionId);
        return row ? { replies: [], safety_status: 'pending', ...row } : null;
    };

    settleSafety = async (sessionId: string, outcome: LarkSafetyOutcome): Promise<void> => {
        if (this.failSettleSafety) throw this.failSettleSafety;
        this.safetySettled.push({ sessionId, outcome });
    };
}

/**
 * 飞书那一头的替身。
 *
 * **不串行化**：`recall` 进来先登记、再等一个可以从外面放行的闸门，所以两条并发的
 * 撤回真的会在里面重叠。串行的替身会把"当前 bot 被另一条消息改掉"这类问题完全掩盖
 * 掉 —— 而那正是出站并发消费下最贵的一种错（从错的人设发出去，文本上看不出来）。
 */
interface ContextSnapshot {
    botName: string;
    lane: string;
    traceId: string;
}

function snapshot(): ContextSnapshot {
    return {
        botName: context.getBotName(),
        lane: context.getLane(),
        traceId: context.getTraceId(),
    };
}

class FakeLarkApi {
    /** 实际调到飞书的那些裸 message id，按调用顺序。 */
    deleted: string[] = [];
    /** 进门那一刻上下文里的 bot / lane / trace。 */
    seenContext: ContextSnapshot[] = [];
    /**
     * **闸门放开之后**再看一次上下文。
     *
     * 只在进门时看是不够的：并发的第二条通常还没起跑，读到的自然是对的。跨过一次
     * await 再看，才能抓住"上下文被后进来的那条改掉了"这种错。
     */
    seenAfterGate: ContextSnapshot[] = [];
    /** 这些 om_id 撤回时抛错。 */
    failFor = new Set<string>();
    /** 装上之后每次 recall 都会停在这里，直到 release()。 */
    private gate: Promise<void> | null = null;
    private open!: () => void;
    /** 已经进到 recall 里面的调用数（含还卡在闸门上的）。 */
    entered = 0;

    hold(): void {
        this.gate = new Promise<void>((resolve) => {
            this.open = resolve;
        });
    }

    release(): void {
        this.open();
        this.gate = null;
    }

    recall = async (messageId: string): Promise<void> => {
        this.entered += 1;
        this.seenContext.push(snapshot());
        if (this.gate) await this.gate;
        this.seenAfterGate.push(snapshot());
        if (this.failFor.has(messageId)) {
            throw new Error(`lark refused to delete ${messageId}`);
        }
        this.deleted.push(messageId);
    };
}

/** 公共层那两张读写：om_id 反查，加上主动开口那条链要的两条。 */
class MemoryRecallTables implements LarkRecallTables {
    /** common_message_id → 飞书 om_id。查不到的就是没映射。 */
    mapping = new Map<string, string>();
    /** 带短横的 agent_outbound_id → 她那次开口落下的行。 */
    spokenRows = new Map<string, LarkProactiveMessageRow[]>();
    /** 反查用过的 agent_outbound_id，逐字记 —— hex 有没有被换成 uuid 就看这里。 */
    lookedUp: string[] = [];
    /** 写下去的撤回时刻。 */
    marked: Array<{ commonMessageId: string; recalledAt: Date }> = [];
    failMarkRecalled?: Error;

    omIdOf = async (commonMessageId: string): Promise<string | null> =>
        this.mapping.get(commonMessageId) ?? null;

    messagesOfAgentOutbound = async (
        agentOutboundId: string,
    ): Promise<LarkProactiveMessageRow[]> => {
        this.lookedUp.push(agentOutboundId);
        return this.spokenRows.get(agentOutboundId) ?? [];
    };

    markRecalled = async (commonMessageId: string, recalledAt: Date): Promise<void> => {
        if (this.failMarkRecalled) throw this.failMarkRecalled;
        this.marked.push({ commonMessageId, recalledAt });
    };
}

interface Harness {
    deps: LarkRecallDeps;
    ledger: MemoryLedger;
    api: FakeLarkApi;
    store: MemoryRecallTables;
    /** common_message_id → 飞书 om_id。查不到的就是没映射。 */
    mapping: Map<string, string>;
    /** speakAs 收到的 who，按调用顺序。 */
    spoke: Array<{ botName: string; lane?: string; traceId?: string }>;
}

const NOW = 1_700_000_000_000;
const NOW_ISO = new Date(NOW).toISOString();

/** 她那次开口的 id：线上是 uuid 的 hex 写法，公共层那一列是 uuid。 */
const OUTBOUND_HEX = '55f3469bd46c5384a9ce22cb4944b77a';
const OUTBOUND_UUID = '55f3469b-d46c-5384-a9ce-22cb4944b77a';

function harness(overrides: Partial<LarkRecallDeps> = {}): Harness {
    const ledger = new MemoryLedger();
    const api = new FakeLarkApi();
    const store = new MemoryRecallTables();
    const spoke: Array<{ botName: string; lane?: string; traceId?: string }> = [];

    const speakAs: LarkSpeakAs = async (who, say) => {
        spoke.push({ ...who });
        await say();
    };

    return {
        ledger,
        api,
        store,
        mapping: store.mapping,
        spoke,
        deps: {
            ledger,
            store,
            api,
            speakAs,
            now: () => NOW,
            ...overrides,
        },
    };
}

function payload(overrides: Partial<LarkRecallPayload> = {}): LarkRecallPayload {
    return {
        channel: 'lark',
        session_id: 'sess-1',
        outbound_id: null,
        reason: 'unsafe',
        detail: '违规内容',
        ...overrides,
    };
}

/** 她自己开口那条链的请求：没有会话，只有一次开口。 */
function proactiveRequest(overrides: Partial<LarkRecallRequest> = {}): LarkRecallRequest {
    return request({
        payload: payload({ session_id: null, outbound_id: OUTBOUND_HEX }),
        ...overrides,
    });
}

function request(overrides: Partial<LarkRecallRequest> = {}): LarkRecallRequest {
    return {
        payload: payload(),
        retryCount: 0,
        ...overrides,
    };
}

function reply(commonMessageId: string): CommonAgentResponseReply {
    return { common_message_id: commonMessageId, sent_at: '2026-08-11T00:00:00.000Z' };
}

/** 等到某件事发生。数微任务轮数是靠不住的 —— 中间隔着几层 await。 */
async function waitFor(predicate: () => boolean, what: string): Promise<void> {
    for (let i = 0; i < 200; i += 1) {
        if (predicate()) return;
        await Bun.sleep(0);
    }
    throw new Error(`timed out waiting for ${what}`);
}

/** 把还能推进的都推进完，用来断言"接下来什么都没再发生"。 */
async function settleAll(): Promise<void> {
    for (let i = 0; i < 20; i += 1) await Bun.sleep(0);
}

// ---------------------------------------------------------------------------
// 逐条撤回与计数
// ---------------------------------------------------------------------------

describe('逐条撤回', () => {
    it('按 replies 的顺序反查 om_id 再撤，全成功就是 recalled', async () => {
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.mapping.set('cm_b', 'om_b');
        h.ledger.seed({
            session_id: 'sess-1',
            bot_name: 'chiwei',
            replies: [reply('cm_a'), reply('cm_b')],
        });

        const outcome = await recallLarkResponse(h.deps, request());

        expect(h.api.deleted).toEqual(['om_a', 'om_b']);
        expect(outcome).toEqual({ kind: 'settled', status: 'recalled', recalled: 2, failed: 0 });
    });

    it('一条撤不掉不中断后续 —— 其余的还是要删', async () => {
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.mapping.set('cm_b', 'om_b');
        h.mapping.set('cm_c', 'om_c');
        h.api.failFor.add('om_b');
        h.ledger.seed({
            session_id: 'sess-1',
            bot_name: 'chiwei',
            replies: [reply('cm_a'), reply('cm_b'), reply('cm_c')],
        });

        const outcome = await recallLarkResponse(h.deps, request());

        expect(h.api.deleted).toEqual(['om_a', 'om_c']);
        expect(outcome).toEqual({ kind: 'settled', status: 'recalled', recalled: 2, failed: 1 });
    });

    it('**只要有一条撤成功，整体就算 recalled**，failed 只进计数和日志', async () => {
        // 这是拆分前的判定口径，照搬：撤回的目的是"违规内容别再挂在群里"，删掉一条
        // 也是往那个方向走了一步，跟"一条都没删掉"不是同一件事。
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.mapping.set('cm_b', 'om_b');
        h.api.failFor.add('om_b');
        h.ledger.seed({
            session_id: 'sess-1',
            bot_name: 'chiwei',
            replies: [reply('cm_a'), reply('cm_b')],
        });

        await recallLarkResponse(h.deps, request());

        expect(h.ledger.safetySettled[0]!.outcome.status).toBe('recalled');
        expect(h.ledger.safetySettled[0]!.outcome.recalled).toBe(1);
        expect(h.ledger.safetySettled[0]!.outcome.failed).toBe(1);
    });

    it('一条都没撤掉才是 recall_failed', async () => {
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.api.failFor.add('om_a');
        h.ledger.seed({ session_id: 'sess-1', bot_name: 'chiwei', replies: [reply('cm_a')] });

        const outcome = await recallLarkResponse(h.deps, request());

        expect(outcome).toEqual({
            kind: 'settled',
            status: 'recall_failed',
            recalled: 0,
            failed: 1,
        });
        expect(h.ledger.safetySettled[0]!.outcome.status).toBe('recall_failed');
    });

    it('反查不到 om_id：计 failed，而且一个飞书 API 都不调', async () => {
        const h = harness();
        h.ledger.seed({ session_id: 'sess-1', bot_name: 'chiwei', replies: [reply('cm_ghost')] });

        const outcome = await recallLarkResponse(h.deps, request());

        expect(h.api.deleted).toEqual([]);
        expect(h.api.entered).toBe(0);
        expect(outcome).toMatchObject({ status: 'recall_failed', recalled: 0, failed: 1 });
    });

    it('逐条串行，不并发 —— 上一条没回来不发下一条', async () => {
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.mapping.set('cm_b', 'om_b');
        h.ledger.seed({
            session_id: 'sess-1',
            bot_name: 'chiwei',
            replies: [reply('cm_a'), reply('cm_b')],
        });

        h.api.hold();
        const running = recallLarkResponse(h.deps, request());
        await waitFor(() => h.api.entered === 1, '第一条进到撤回里');

        // 第一条还卡着，第二条就不该被发出去。
        await settleAll();
        expect(h.api.entered).toBe(1);

        h.api.release();
        await running;
        expect(h.api.entered).toBe(2);
    });
});

// ---------------------------------------------------------------------------
// 安全终态
// ---------------------------------------------------------------------------

describe('安全终态 — safety_status 与 safety_result', () => {
    it('reason / detail 原样带回，计数与 checked_at 由本次撤回填', async () => {
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.mapping.set('cm_b', 'om_b');
        h.api.failFor.add('om_b');
        h.ledger.seed({
            session_id: 'sess-1',
            bot_name: 'chiwei',
            replies: [reply('cm_a'), reply('cm_b')],
        });

        await recallLarkResponse(
            h.deps,
            request({ payload: payload({ reason: 'porn', detail: '模型判定' }) }),
        );

        expect(h.ledger.safetySettled).toEqual([
            {
                sessionId: 'sess-1',
                outcome: {
                    status: 'recalled',
                    reason: 'porn',
                    detail: '模型判定',
                    recalled: 1,
                    failed: 1,
                    checkedAt: NOW_ISO,
                },
            },
        ]);
    });

    it('没有 detail 时不编一个', async () => {
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.ledger.seed({ session_id: 'sess-1', bot_name: 'chiwei', replies: [reply('cm_a')] });

        await recallLarkResponse(
            h.deps,
            request({ payload: payload({ detail: undefined }) }),
        );

        expect(h.ledger.safetySettled[0]!.outcome.detail).toBeUndefined();
    });

    it('写终态自己失败：往外抛，不吞', async () => {
        // 拆分前就是这个形态：消息已经撤了，但台账没写上 —— 抛出去让消息进 DLQ，
        // 至少留得下痕迹。吞掉的话这一行会永远停在 pending，没人知道撤过了。
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.ledger.seed({ session_id: 'sess-1', bot_name: 'chiwei', replies: [reply('cm_a')] });
        h.ledger.failSettleSafety = new Error('pg is down');

        await expect(recallLarkResponse(h.deps, request())).rejects.toThrow('pg is down');
        expect(h.api.deleted).toEqual(['om_a']);
    });
});

// ---------------------------------------------------------------------------
// 终态短路
// ---------------------------------------------------------------------------

describe('终态短路 — 已经落过终态就不再撤一次', () => {
    for (const status of ['recalled', 'recall_failed']) {
        it(`safety_status 已经是 ${status}：一个 API 不调、一列不写`, async () => {
            // 重复撤回会把 recalled 覆盖成 recall_failed（消息已经没了，第二次删必然
            // 失败）。agent-service 那侧的 TERMINAL_STATUSES 短路正是假设撤回这一侧
            // 不会改写终态，这里对称做一次入口检查。
            const h = harness();
            h.mapping.set('cm_a', 'om_a');
            h.ledger.seed({
                session_id: 'sess-1',
                bot_name: 'chiwei',
                replies: [reply('cm_a')],
                safety_status: status,
            });

            const outcome = await recallLarkResponse(h.deps, request());

            expect(outcome).toEqual({ kind: 'short-circuited', status });
            expect(h.api.entered).toBe(0);
            expect(h.ledger.safetySettled).toEqual([]);
        });
    }

    it('其它 safety_status（pending / blocked）不短路，照常撤', async () => {
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.ledger.seed({
            session_id: 'sess-1',
            bot_name: 'chiwei',
            replies: [reply('cm_a')],
            safety_status: 'blocked',
        });

        const outcome = await recallLarkResponse(h.deps, request());

        expect(outcome).toMatchObject({ kind: 'settled', status: 'recalled' });
    });
});

// ---------------------------------------------------------------------------
// 延时重投
// ---------------------------------------------------------------------------

describe('台账还没落 replies — 延时重投', () => {
    it('台账行压根不存在：重投，不判定为无事可做', async () => {
        const h = harness();

        const outcome = await recallLarkResponse(h.deps, request());

        expect(outcome).toEqual({ kind: 'retry', delayMs: 5000, retryCount: 1 });
        expect(h.ledger.safetySettled).toEqual([]);
    });

    it('行在但 replies 是空数组：同样重投（典型 race — 撤回跑赢了出站落库）', async () => {
        const h = harness();
        h.ledger.seed({ session_id: 'sess-1', bot_name: 'chiwei', replies: [] });

        const outcome = await recallLarkResponse(h.deps, request());

        expect(outcome).toEqual({ kind: 'retry', delayMs: 5000, retryCount: 1 });
    });

    it('退避表 5s / 10s / 15s，计数逐次加一', async () => {
        for (const [retryCount, delayMs] of [
            [0, 5000],
            [1, 10000],
            [2, 15000],
        ] as const) {
            const h = harness();
            const outcome = await recallLarkResponse(h.deps, request({ retryCount }));
            expect(outcome).toEqual({ kind: 'retry', delayMs, retryCount: retryCount + 1 });
        }
    });

    it('重投到顶（第 3 次仍然没有 replies）：写 recall_failed 终态再交给死信', async () => {
        // 不写这一笔的话 status 永远停在 pending，台账上看不出这次撤回失败过。
        const h = harness();

        const outcome = await recallLarkResponse(h.deps, request({ retryCount: 3 }));

        expect(outcome).toEqual({ kind: 'exhausted' });
        expect(h.ledger.safetySettled).toEqual([
            {
                sessionId: 'sess-1',
                outcome: {
                    status: 'recall_failed',
                    reason: 'unsafe',
                    detail: '违规内容',
                    recalled: 0,
                    failed: 0,
                    checkedAt: NOW_ISO,
                },
            },
        ]);
    });

    it('重投到顶时写终态失败：吞掉，仍然进死信', async () => {
        // 这一次的写入是"顺手补一笔"，它失败不该盖掉"这条消息要进 DLQ"这个结论。
        const h = harness();
        h.ledger.failSettleSafety = new Error('pg is down');

        const outcome = await recallLarkResponse(h.deps, request({ retryCount: 3 }));

        expect(outcome).toEqual({ kind: 'exhausted' });
    });
});

// ---------------------------------------------------------------------------
// 谁在撤
// ---------------------------------------------------------------------------

describe('撤回跑在哪个 bot 的上下文里', () => {
    it('用台账上的 bot_name，并带上入站的 lane 与 trace', async () => {
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.ledger.seed({ session_id: 'sess-1', bot_name: 'chiwei', replies: [reply('cm_a')] });

        await recallLarkResponse(
            h.deps,
            request({ lane: 'ppe-x', traceId: 'trace-inbound' }),
        );

        expect(h.spoke).toEqual([
            { botName: 'chiwei', lane: 'ppe-x', traceId: 'trace-inbound' },
        ]);
    });

    it('台账上没有 bot_name：照样往下走 —— 客户端池会拒绝猜，逐条记 failed', async () => {
        // 拆分前逐字相同：撤回不因为没有 bot 就跳过，而是让飞书那一跳自己炸掉，
        // 于是终态是 recall_failed 而不是"什么都没发生"。
        const h = harness();
        h.mapping.set('cm_a', 'om_a');
        h.ledger.seed({ session_id: 'sess-1', replies: [reply('cm_a')] });
        h.api.failFor.add('om_a');

        const outcome = await recallLarkResponse(h.deps, request());

        expect(h.spoke[0]!.botName).toBe('');
        expect(outcome).toMatchObject({ status: 'recall_failed', failed: 1 });
    });

    it('两条撤回并发跑：各看各的 bot，不互相串', async () => {
        // 出站是竞争消费，同一个进程里几条消息交错跑。用进程级的"当前 bot"字段的话
        // 后进来的那条会改掉前一条的值，撤回就打到别的飞书应用上去（那边根本没有这条
        // 消息，于是全部 failed）。这里接的是**真的** larkSpeakAs。
        const one = harness({ speakAs: larkSpeakAs });
        const two = harness({ speakAs: larkSpeakAs });
        for (const h of [one, two]) {
            h.mapping.set('cm_a', 'om_a');
        }
        one.ledger.seed({ session_id: 'sess-1', bot_name: '赤尾', replies: [reply('cm_a')] });
        two.ledger.seed({ session_id: 'sess-2', bot_name: '小黑', replies: [reply('cm_a')] });

        one.api.hold();
        two.api.hold();
        const running = Promise.all([
            recallLarkResponse(one.deps, request({ lane: 'ppe-one', traceId: 'trace-one' })),
            recallLarkResponse(
                two.deps,
                request({ payload: payload({ session_id: 'sess-2' }), lane: 'ppe-two' }),
            ),
        ]);
        // 两条都已经进到 recall 里面、都还没返回 —— 真的重叠了。
        await waitFor(
            () => one.api.entered === 1 && two.api.entered === 1,
            '两条撤回同时卡在飞书那一跳上',
        );

        one.api.release();
        two.api.release();
        await running;

        // 跨过 await 之后再看：这时两条都已经进来过了，上下文串了就会在这里露出来。
        expect(one.api.seenAfterGate).toEqual([
            { botName: '赤尾', lane: 'ppe-one', traceId: 'trace-one' },
        ]);
        expect(two.api.seenAfterGate[0]!.botName).toBe('小黑');
        expect(two.api.seenAfterGate[0]!.lane).toBe('ppe-two');
    });

    it('没给 traceId 时自己铸一条 —— 这次处理内部至少是自洽的一条链', async () => {
        const h = harness({ speakAs: larkSpeakAs });
        h.mapping.set('cm_a', 'om_a');
        h.ledger.seed({ session_id: 'sess-1', bot_name: 'chiwei', replies: [reply('cm_a')] });

        await recallLarkResponse(h.deps, request({ traceId: undefined }));

        expect(h.api.seenContext[0]!.traceId).not.toBe('');
    });
});

// ---------------------------------------------------------------------------
// 她自己开口那条：按 outbound_id 撤
// ---------------------------------------------------------------------------

/** 她那次开口在公共层落下的一行。 */
function spoken(
    commonMessageId: string,
    botName: string | null = 'chiwei',
    recalledAt: Date | null = null,
): LarkProactiveMessageRow {
    return {
        common_message_id: commonMessageId,
        bot_name: botName,
        recalled_at: recalledAt,
    };
}

describe('撤她主动发的那条消息 — 按 outbound_id', () => {
    it('hex 的 outbound_id 换成 uuid 去反查，撤掉那条消息并记下撤回时刻', async () => {
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        // 线上给的是 32 个字符没有短横的 hex，公共层那一列是 uuid。
        expect(h.store.lookedUp).toEqual([OUTBOUND_UUID]);
        expect(h.api.deleted).toEqual(['om_p']);
        expect(h.store.marked).toEqual([
            { commonMessageId: 'cm_p', recalledAt: new Date(NOW) },
        ]);
        expect(outcome).toEqual({ kind: 'settled', status: 'recalled', recalled: 1, failed: 0 });
    });

    it('一行台账都不碰 —— 主动消息在那张表上压根没有行', async () => {
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');

        await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.ledger.looked).toEqual([]);
        expect(h.ledger.safetySettled).toEqual([]);
    });

    it('那次开口发了好几段：逐段撤，各记各的撤回时刻', async () => {
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p0'), spoken('cm_p1')]);
        h.mapping.set('cm_p0', 'om_p0');
        h.mapping.set('cm_p1', 'om_p1');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.api.deleted).toEqual(['om_p0', 'om_p1']);
        expect(h.store.marked.map((m) => m.commonMessageId)).toEqual(['cm_p0', 'cm_p1']);
        expect(outcome).toMatchObject({ status: 'recalled', recalled: 2, failed: 0 });
    });

    it('用消息行上的 bot 说话，并带上入站的 lane 与 trace', async () => {
        // 飞书只让发送者撤自己的消息。主动发没有台账行，这个身份只能从消息行上拿 ——
        // 出站落库时写进去的正是当时说话的那个 bot。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p', '赤尾')]);
        h.mapping.set('cm_p', 'om_p');

        await recallLarkResponse(
            h.deps,
            proactiveRequest({ lane: 'ppe-x', traceId: 'trace-inbound' }),
        );

        expect(h.spoke).toEqual([{ botName: '赤尾', lane: 'ppe-x', traceId: 'trace-inbound' }]);
    });

    it('行上没有 bot_name：照样往下走 —— 客户端池会拒绝猜，记 failed', async () => {
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p', null)]);
        h.mapping.set('cm_p', 'om_p');
        h.api.failFor.add('om_p');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.spoke[0]!.botName).toBe('');
        expect(outcome).toMatchObject({ status: 'recall_failed', failed: 1 });
    });
});

describe('撤主动消息 — 撤不掉的时候不许写 recalled_at', () => {
    it('飞书拒绝删除：不写撤回时刻', async () => {
        // 写了就是告诉她"收回去了"，而那句话还挂在群里。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');
        h.api.failFor.add('om_p');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.store.marked).toEqual([]);
        expect(outcome).toEqual({
            kind: 'settled',
            status: 'recall_failed',
            recalled: 0,
            failed: 1,
        });
    });

    it('om_id 是合成的假键：一个飞书接口都不调，也不写撤回时刻', async () => {
        // 飞书偶尔返回成功却不给消息标识，那时落库的是 `<这次开口的 id>_part{段序}`
        //（见 deliver.ts）。拿它去调删除接口必定失败，先认出来比白打一次强。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', `${OUTBOUND_UUID}_part0`);

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.api.entered).toBe(0);
        expect(h.store.marked).toEqual([]);
        expect(outcome).toMatchObject({ status: 'recall_failed', recalled: 0, failed: 1 });
    });

    it('反查不到 om_id：计 failed，一个飞书接口都不调', async () => {
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.api.entered).toBe(0);
        expect(h.store.marked).toEqual([]);
        expect(outcome).toMatchObject({ status: 'recall_failed', failed: 1 });
    });

    it('多段里撤掉一段、另一段撤不掉：只有撤掉的那一段记撤回时刻', async () => {
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p0'), spoken('cm_p1')]);
        h.mapping.set('cm_p0', 'om_p0');
        h.mapping.set('cm_p1', 'om_p1');
        h.api.failFor.add('om_p0');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.store.marked.map((m) => m.commonMessageId)).toEqual(['cm_p1']);
        expect(outcome).toMatchObject({ status: 'recalled', recalled: 1, failed: 1 });
    });

});

// ---------------------------------------------------------------------------
// 删掉了、却没记下撤回时刻
// ---------------------------------------------------------------------------

describe('撤主动消息 — 删掉了但没记下撤回时刻', () => {
    it('不当成撤完了：延时重投，让这条还有第二次机会', async () => {
        // 飞书那条消息此刻已经没了，公共层还说它在。上游（她手机上那份）就是按
        // recalled_at 判"这句话还在不在"的，所以这一刻库里是假的 —— 她会看见一条
        // 实际上不存在的消息，还可能基于它说话。
        //
        // 判成 settled 就等于 ACK，这条消息从队列上消失，再也没有第二次机会 ——
        // 剩下的只有一行日志。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');
        h.store.failMarkRecalled = new Error('pg is down');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.api.deleted).toEqual(['om_p']);
        expect(outcome).toEqual({ kind: 'retry', delayMs: 5000, retryCount: 1 });
    });

    it('重投回来时那一列已经非空：不再删一次，算撤掉了', async () => {
        // 上一次其实写成功了（连接在返回的路上断了、或者另一条撤回抢先写了）。这一列
        // 是主动消息唯一的终态记录，读到就直接认，省掉一次白打的删除调用 —— 飞书对
        // 一条已经删掉的消息返回非 0 code（见 lark-api.ts 的 recall）。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [
            spoken('cm_p', 'chiwei', new Date(NOW - 5000)),
        ]);
        h.mapping.set('cm_p', 'om_p');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest({ retryCount: 1 }));

        expect(h.api.entered).toBe(0);
        expect(h.store.marked).toEqual([]);
        expect(outcome).toEqual({ kind: 'settled', status: 'recalled', recalled: 1, failed: 0 });
    });

    it('重投到顶还是写不下去：交给死信 —— 留一条能把它补上的路', async () => {
        // 死信里那条能查、能重放；一行日志不能。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');
        h.store.failMarkRecalled = new Error('pg is down');

        const outcome = await recallLarkResponse(
            h.deps,
            proactiveRequest({ retryCount: 3 }),
        );

        expect(h.api.deleted).toEqual(['om_p']);
        expect(outcome).toEqual({ kind: 'exhausted' });
    });

    it('多段里一段记上了、另一段没记上：整条重投，不报成撤完了', async () => {
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p0'), spoken('cm_p1')]);
        h.mapping.set('cm_p0', 'om_p0');
        h.mapping.set('cm_p1', 'om_p1');
        // 第一段写得下去，第二段写的时候库挂了。
        let written = 0;
        h.store.markRecalled = async (commonMessageId, recalledAt) => {
            written += 1;
            if (written > 1) throw new Error('pg is down');
            h.store.marked.push({ commonMessageId, recalledAt });
        };

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.api.deleted).toEqual(['om_p0', 'om_p1']);
        expect(h.store.marked.map((m) => m.commonMessageId)).toEqual(['cm_p0']);
        expect(outcome).toEqual({ kind: 'retry', delayMs: 5000, retryCount: 1 });
    });

    it('重投回来时已经记上的那一段不重删，只补没记上的那一段', async () => {
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [
            spoken('cm_p0', 'chiwei', new Date(NOW - 5000)),
            spoken('cm_p1'),
        ]);
        h.mapping.set('cm_p0', 'om_p0');
        h.mapping.set('cm_p1', 'om_p1');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest({ retryCount: 1 }));

        // 第一段已经没了，不必再问飞书一趟。
        expect(h.api.deleted).toEqual(['om_p1']);
        expect(h.store.marked.map((m) => m.commonMessageId)).toEqual(['cm_p1']);
        expect(outcome).toEqual({ kind: 'settled', status: 'recalled', recalled: 2, failed: 0 });
    });

    it('每一段都已经记过撤回时刻：一个飞书接口都不调，直接算撤完了', async () => {
        // 同一条撤回被投递两次（并发消费、或者死信重放）时的样子。照会话那条链的
        // 终态短路来 —— 主动消息没有台账行，这一列就是它唯一的终态。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [
            spoken('cm_p0', 'chiwei', new Date(NOW - 5000)),
            spoken('cm_p1', 'chiwei', new Date(NOW - 4000)),
        ]);
        h.mapping.set('cm_p0', 'om_p0');
        h.mapping.set('cm_p1', 'om_p1');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.api.entered).toBe(0);
        expect(h.store.marked).toEqual([]);
        expect(outcome).toEqual({ kind: 'settled', status: 'recalled', recalled: 2, failed: 0 });
    });
});

// ---------------------------------------------------------------------------
// 飞书说「这条消息已经被撤回或删除」
// ---------------------------------------------------------------------------

/** 一个飞书判的失败，带着它那个数字码 —— 跟 LarkClient 抛出来的形状一样。 */
function larkRefusal(message: string, code: number): Error {
    return withLarkErrorCode(new Error(message), code);
}

describe('撤主动消息 — 飞书说这条已经被撤回或删除', () => {
    it('认成"真人已经看不到它"：写 recalled_at、计 recalled，不计 failed', async () => {
        // 她撤的是她自己发出去的消息，而飞书只允许发送者撤回自己的消息 —— 这条不可能
        // 是被别人撤掉的。收到这个码只有两种来源：上一次撤成功了（写 recalled_at 那步
        // 没落上），或者这一次之前它已经被撤过。两种都意味着真人现在看不到它。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');
        h.api.recall = async () => {
            throw larkRefusal('消息已被撤回或删除', 99991663);
        };

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.store.marked).toEqual([{ commonMessageId: 'cm_p', recalledAt: new Date(NOW) }]);
        expect(outcome).toEqual({ kind: 'settled', status: 'recalled', recalled: 1, failed: 0 });
    });

    it('别的非 0 码仍然是撤不掉：不写 recalled_at，计 failed', async () => {
        // 超出飞书撤回时限、机器人已经不在这个群里 —— 这些都是消息还挂在群里。一并
        // 当成功会把"撤不掉"记成"撤掉了"，她据此以为那句话没了，而它还在。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');
        h.api.recall = async () => {
            throw larkRefusal('机器人不在群聊中', 99991668);
        };

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(h.store.marked).toEqual([]);
        expect(outcome).toEqual({
            kind: 'settled',
            status: 'recall_failed',
            recalled: 0,
            failed: 1,
        });
    });

    it('写 recalled_at 这一步照样可能失败，那还是 unrecorded —— 重投', async () => {
        // 认了这个码只是说"删这件事不用再做了"，不是说"记这件事已经做完了"。
        const h = harness();
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');
        h.api.recall = async () => {
            throw larkRefusal('消息已被撤回或删除', 99991663);
        };
        h.store.failMarkRecalled = new Error('pg is down');

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(outcome).toEqual({ kind: 'retry', delayMs: 5000, retryCount: 1 });
    });
});

describe('缺口闭合 — 删成功、写库失败、重投之后飞书说它已经不在了', () => {
    it('第一轮删掉却没记上 → 重投 → 第二轮飞书返回已撤 → 认成撤掉了并记上', async () => {
        // 一条走完整条路。关键在两层之间：飞书那个数字码要穿过 LarkClient 的错误构造、
        // 穿过出站端口，才到得了 recall.ts —— 所以这里接的是**真的** LarkClient 和
        // 真的 createSdkLarkApi，只把最外面那个原生 SDK client 换成手写替身。
        //
        // 没有这条路的话，重投永远收敛不了：第二次删必然被飞书拒绝、记成 failed 并
        // ACK，recalled_at 永远是 NULL，她手机上那条消息就一直读作"还在"。
        let deleted = 0;
        const native = {
            im: {
                message: {
                    delete: async () => {
                        deleted += 1;
                        // 第一次真的删掉了；第二次飞书说这条消息已经不在。
                        return deleted === 1
                            ? { code: 0, msg: 'ok', data: {} }
                            : { code: 99991663, msg: 'message has been deleted' };
                    },
                },
            },
        };
        const larkClient = new LarkClient({ appId: 'cli_test', appSecret: 'secret' });
        (larkClient as unknown as { client: unknown }).client = native;

        const h = harness({ api: createSdkLarkApi({ current: () => larkClient }) });
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_p', 'om_p');

        // 第一轮：飞书那条消息删掉了，写 recalled_at 的时候库挂了。
        h.store.failMarkRecalled = new Error('pg is down');
        const first = await recallLarkResponse(h.deps, proactiveRequest());

        expect(deleted).toBe(1);
        expect(h.store.marked).toEqual([]);
        expect(first).toEqual({ kind: 'retry', delayMs: 5000, retryCount: 1 });

        // 第二轮：库好了，但 recalled_at 仍然是 NULL（上一次没写上），所以那一列的
        // 短路不生效 —— 还会再删一次，而飞书对一条已经删掉的消息返回非 0 code。
        h.store.failMarkRecalled = undefined;
        const second = await recallLarkResponse(h.deps, proactiveRequest({ retryCount: 1 }));

        expect(deleted).toBe(2);
        expect(h.store.marked).toEqual([{ commonMessageId: 'cm_p', recalledAt: new Date(NOW) }]);
        expect(second).toEqual({ kind: 'settled', status: 'recalled', recalled: 1, failed: 0 });
    });
});

describe('撤主动消息 — 投递方还没落库', () => {
    it('反查不到那次开口：延时重投，不判定为无事可做', async () => {
        // 她可能刚说完就想撤，那一刻投递方还没把这条消息写进公共层。判成"没有东西
        // 要撤"的话，那句话就永远留在群里了。
        const h = harness();

        const outcome = await recallLarkResponse(h.deps, proactiveRequest());

        expect(outcome).toEqual({ kind: 'retry', delayMs: 5000, retryCount: 1 });
        expect(h.api.entered).toBe(0);
        expect(h.store.marked).toEqual([]);
    });

    it('退避表跟会话那条链共用：5s / 10s / 15s', async () => {
        for (const [retryCount, delayMs] of [
            [0, 5000],
            [1, 10000],
            [2, 15000],
        ] as const) {
            const h = harness();
            const outcome = await recallLarkResponse(h.deps, proactiveRequest({ retryCount }));
            expect(outcome).toEqual({ kind: 'retry', delayMs, retryCount: retryCount + 1 });
        }
    });

    it('重投到顶还是查不到：交给死信，一行台账都不写', async () => {
        // 会话那条链此时会补一笔 recall_failed 到台账上；主动消息没有台账行，
        // 唯一能留下的痕迹是死信和日志。
        const h = harness();

        const outcome = await recallLarkResponse(h.deps, proactiveRequest({ retryCount: 3 }));

        expect(outcome).toEqual({ kind: 'exhausted' });
        expect(h.ledger.safetySettled).toEqual([]);
    });
});

describe('撤回请求指向什么 — 两种定位方式恰好用一种', () => {
    it('outbound_id 不是 uuid 的 hex 写法：一个库都不查、一个接口都不调，交给死信', async () => {
        // 塞进 SQL 会让 PG 抛 invalid input syntax，那个异常从消费者 handler 里穿出去，
        // 这条消息的处置就不再由本文件决定。
        const h = harness();

        const outcome = await recallLarkResponse(
            h.deps,
            request({ payload: payload({ session_id: null, outbound_id: 'not-a-uuid' }) }),
        );

        expect(outcome).toEqual({ kind: 'exhausted' });
        expect(h.store.lookedUp).toEqual([]);
        expect(h.ledger.looked).toEqual([]);
        expect(h.api.entered).toBe(0);
    });

    it('两个定位方式都给了：不猜用哪个，交给死信', async () => {
        // 生产者那一侧在构造时就判了"恰好一个"，真收到一条说明有人绕过了那条路径。
        const h = harness();
        h.ledger.seed({ session_id: 'sess-1', bot_name: 'chiwei', replies: [reply('cm_a')] });
        h.store.spokenRows.set(OUTBOUND_UUID, [spoken('cm_p')]);
        h.mapping.set('cm_a', 'om_a');
        h.mapping.set('cm_p', 'om_p');

        const outcome = await recallLarkResponse(
            h.deps,
            request({ payload: payload({ session_id: 'sess-1', outbound_id: OUTBOUND_HEX }) }),
        );

        expect(outcome).toEqual({ kind: 'exhausted' });
        expect(h.api.entered).toBe(0);
        expect(h.ledger.looked).toEqual([]);
        expect(h.store.lookedUp).toEqual([]);
    });

    it('一个都没给：同样交给死信，不拿空串去查台账', async () => {
        const h = harness();

        const outcome = await recallLarkResponse(
            h.deps,
            request({ payload: payload({ session_id: null, outbound_id: null }) }),
        );

        expect(outcome).toEqual({ kind: 'exhausted' });
        expect(h.ledger.looked).toEqual([]);
        expect(h.store.lookedUp).toEqual([]);
    });
});
