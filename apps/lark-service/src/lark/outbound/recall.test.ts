// 安全审计判违规之后，把已经发出去的那几条飞书消息删掉，并在台账上落安全终态。
//
// 这些用例是拆分前 channel-server recall-worker 那条链的行为基线：终态短路、逐条撤回
// 的计数语义、台账还没落 replies 时的延时重投、重投到顶之后的终态。
//
// 队列那一头（订阅、fail-closed、ACK、重投怎么发出去）在 recall-queue.test.ts。

import { describe, expect, it } from 'bun:test';
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

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

/** 种进台账的行：不写的列取默认值。 */
type LedgerSeed = Partial<LarkAgentResponseRow> & { session_id: string };

class MemoryLedger {
    rows = new Map<string, LedgerSeed>();
    safetySettled: Array<{ sessionId: string; outcome: LarkSafetyOutcome }> = [];
    failSettleSafety?: Error;

    seed(seed: LedgerSeed): void {
        this.rows.set(seed.session_id, seed);
    }

    find = async (sessionId: string): Promise<LarkAgentResponseRow | null> => {
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

interface Harness {
    deps: LarkRecallDeps;
    ledger: MemoryLedger;
    api: FakeLarkApi;
    /** common_message_id → 飞书 om_id。查不到的就是没映射。 */
    mapping: Map<string, string>;
    /** speakAs 收到的 who，按调用顺序。 */
    spoke: Array<{ botName: string; lane?: string; traceId?: string }>;
}

const NOW = 1_700_000_000_000;
const NOW_ISO = new Date(NOW).toISOString();

function harness(overrides: Partial<LarkRecallDeps> = {}): Harness {
    const ledger = new MemoryLedger();
    const api = new FakeLarkApi();
    const mapping = new Map<string, string>();
    const spoke: Array<{ botName: string; lane?: string; traceId?: string }> = [];

    const speakAs: LarkSpeakAs = async (who, say) => {
        spoke.push({ ...who });
        await say();
    };

    return {
        ledger,
        api,
        mapping,
        spoke,
        deps: {
            ledger,
            store: { omIdOf: async (commonMessageId) => mapping.get(commonMessageId) ?? null },
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
        reason: 'unsafe',
        detail: '违规内容',
        ...overrides,
    };
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
