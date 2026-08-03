import { describe, it, expect } from 'bun:test';

import { handleRecall } from './recall-worker';
import type { RecallHandlerDeps } from './recall-worker';
import { context } from '@middleware/context';
import type { OutboundCapabilities, MessageRef } from '@inner/shared/channel';
import type { ConsumeMessage } from 'amqplib';

// recall-worker 的 lane 恢复口径，与 chat-response-handler 同源（见那边的长注释）：
// lane 只认 AMQP header `lane`，空串 / 非字符串 = 无 lane（prod），不回落 body.lane、
// 不回落 env LANE。recall 队列同样带 10s TTL + DLX，泳道消息会降级回 prod 队列由
// prod worker 接手，撤回必须发生在原泳道的 context 下。
//
// 这里额外钉住重投路径：replies 还没落库时 worker 会延时重投一条 recall，重投必须
// 沿用入站 header 解析出的 lane，否则一次重投就把 lane 丢干净（下一跳再也无从恢复）。
//
// 但 handler 只负责把 lane 作为 republish 的**第四个参数**传对——lane header 本身由
// rabbitmq.ts::publish 内部按这个参数统一注入（rabbitmq.test.ts 覆盖：显式 lane 参数
// → header，'prod' → 空串，调用方自带的 x-retry-count 不被覆盖）。所以下面断言
// headers 里**没有** lane：两处都写 lane header 会让「谁负责注入」变模糊，且两份口径
// 迟早漂移。

function makeMsg(
    payload: Record<string, unknown>,
    headers?: Record<string, unknown>,
): ConsumeMessage {
    return {
        content: Buffer.from(JSON.stringify(payload)),
        fields: {} as ConsumeMessage['fields'],
        properties: { headers } as unknown as ConsumeMessage['properties'],
    } as ConsumeMessage;
}

function recallPayload(bodyLane?: string) {
    return {
        channel: 'lark',
        session_id: 'sess-1',
        reason: 'unsafe',
        detail: 'test',
        ...(bodyLane === undefined ? {} : { lane: bodyLane }),
    };
}

interface Harness {
    deps: RecallHandlerDeps;
    observedLanes: Array<string | undefined>;
    republishes: Array<{
        payload: Record<string, unknown>;
        delayMs: number;
        headers: Record<string, unknown>;
        lane: string;
    }>;
}

// hasReplies=false 模拟「replies 还没落库」→ 走延时重投分支。
function makeHarness(hasReplies: boolean): Harness {
    const observedLanes: Array<string | undefined> = [];
    const republishes: Harness['republishes'] = [];

    const cap: OutboundCapabilities = {
        async resolveOutboundTarget() {
            throw new Error('not used');
        },
        async resolveMessageRef(): Promise<MessageRef> {
            return { channelId: 'om_to_recall' };
        },
        async resolveConversationRef() {
            return { channelId: 'oc_x' };
        },
        async recordOutboundMessage() {
            return 'ignored';
        },
        async sendText(): Promise<MessageRef> {
            return { channelId: 'ignored' };
        },
        async reply(): Promise<MessageRef> {
            return { channelId: 'ignored' };
        },
        // 撤回在 context.run 内部执行：这里观测 handler 实际取到的 lane。
        async recall() {
            observedLanes.push(context.getAll().lane);
        },
    };

    const repo = {
        findOneBy: async () =>
            hasReplies
                ? {
                      session_id: 'sess-1',
                      bot_name: 'akao',
                      safety_status: 'pending',
                      replies: [{ common_message_id: 'cm-1' }],
                  }
                : null,
        update: async () => ({ affected: 1 }),
    } as unknown as RecallHandlerDeps['repo'];

    return {
        observedLanes,
        republishes,
        deps: {
            repo,
            getCapabilities: () => cap,
            republish: async (payload, delayMs, headers, lane) => {
                republishes.push({ payload, delayMs, headers, lane });
            },
            ack: () => {},
            nack: () => {},
        },
    };
}

describe('handleRecall — lane 从 AMQP header 恢复', () => {
    it('header 带真实 lane：撤回发生在该 lane 的 context 下', async () => {
        const h = makeHarness(true);

        await handleRecall(h.deps, makeMsg(recallPayload(), { lane: 'ppe-taskb' }));

        expect(h.observedLanes).toEqual(['ppe-taskb']);
    });

    it('header lane 为空串：视为无 lane（prod），不回落 body.lane', async () => {
        const h = makeHarness(true);

        await handleRecall(h.deps, makeMsg(recallPayload('ppe-taskb'), { lane: '' }));

        expect(h.observedLanes).toEqual([undefined]);
    });

    it('完全没有 header：视为无 lane（prod），不回落 body.lane', async () => {
        const h = makeHarness(true);

        await handleRecall(h.deps, makeMsg(recallPayload('ppe-taskb'), undefined));

        expect(h.observedLanes).toEqual([undefined]);
    });

    it('header 与 body 的 lane 不一致：header 是唯一权威', async () => {
        const h = makeHarness(true);

        await handleRecall(h.deps, makeMsg(recallPayload('ppe-stale'), { lane: 'ppe-taskb' }));

        expect(h.observedLanes).toEqual(['ppe-taskb']);
    });
});

describe('handleRecall — 重投沿用 header lane', () => {
    it('replies 未落库重投：目标 lane 用入站 header 的 lane，lane header 交给 publish 注入', async () => {
        const h = makeHarness(false);

        await handleRecall(h.deps, makeMsg(recallPayload('ppe-stale'), { lane: 'ppe-taskb' }));

        expect(h.republishes.length).toBe(1);
        expect(h.republishes[0].lane).toBe('ppe-taskb');
        // headers 只带重试计数：lane header 由 publish 按上面这个 lane 参数注入，
        // handler 不重复写一份。toEqual 全量比对，防止 lane 被重新塞回来。
        expect(h.republishes[0].headers).toEqual({ 'x-retry-count': 1 });
    });

    it('无 header 的重投：显式投回 prod（publish 据此把 lane header 写成空串）', async () => {
        const h = makeHarness(false);

        await handleRecall(h.deps, makeMsg(recallPayload('ppe-stale'), undefined));

        expect(h.republishes.length).toBe(1);
        // 'prod' 是显式值而非 undefined：publish 对 undefined 会回落 env LANE，
        // 那正是这里绝不能发生的事。
        expect(h.republishes[0].lane).toBe('prod');
        expect(h.republishes[0].headers).toEqual({ 'x-retry-count': 1 });
    });

    it('重投沿用入站的 x-retry-count（第 2 次重投计数为 2）', async () => {
        const h = makeHarness(false);

        await handleRecall(
            h.deps,
            makeMsg(recallPayload(), { lane: 'ppe-taskb', 'x-retry-count': 1 }),
        );

        expect(h.republishes[0].headers).toEqual({ 'x-retry-count': 2 });
        expect(h.republishes[0].lane).toBe('ppe-taskb');
    });
});
