import { describe, it, expect, beforeEach, mock } from 'bun:test';

// dispatchLarkEvent 是飞书事件进入本进程入站链路的唯一收口：审计落库 +
// 在 bot context 内按 event_type 找 handler 异步执行。webhook 入口调它，
// 避免两份重复的分发逻辑。
//
// 这里只验它的分发契约（找到 handler→调用、找不到→不抛、审计被调用），
// 不碰真实 mongo / handlers。

const insertEventCalls: unknown[] = [];
mock.module('@dal/mongo/client', () => ({
    insertEvent: async (e: unknown) => {
        insertEventCalls.push(e);
    },
}));

const handlerCalls: Array<{ type: string; params: unknown }> = [];
let registered: Record<string, ((p: unknown) => Promise<void>) | undefined> = {};
type TestContext = { botName?: string; traceId?: string; lane?: string };
let activeContext: TestContext = {};
mock.module('@lark/events/event-registry', () => ({
    EventHandler: () => () => undefined,
    EventRegistry: {
        getHandlerByEventType: (t: string) => registered[t],
    },
    registerEventHandlerInstance: () => {},
}));
mock.module('@lark/events/handlers', () => ({
    larkEventHandlers: {},
}));
mock.module('@middleware/context', () => ({
    context: {
        createContext: (botName?: string, traceId?: string, lane?: string) => ({
            botName,
            traceId: traceId ?? 't',
            lane,
        }),
        run: async (ctx: TestContext, cb: () => Promise<unknown>) => {
            activeContext = ctx;
            return cb();
        },
        getBotName: () => activeContext.botName,
        getLane: () => activeContext.lane,
    },
}));
mock.module('@aliyun/oss', () => ({
    getOss: () => ({ getFile: mock(async () => undefined) }),
}));
mock.module('@cache/redis-client', () => ({
    hgetall: mock(async () => ({})),
    setNx: mock(async () => 'OK'),
    evalScript: mock(async () => 1),
    exists: mock(async () => 0),
}));
mock.module('@infrastructure/lane-router', () => ({
    laneRouter: { createClient: () => ({ post: mock(async () => undefined) }) },
}));
mock.module('@plugins/lark/commands', () => ({ larkCommands: [] }));

// 其他 webhook 测试会 mock.module('./dispatch') 来隔离 ingress glue。bun 的
// module mock 是进程级的，这里用绝对 file URL 强制加载真实 dispatch 实现。
const REAL_DISPATCH = new URL('./dispatch.ts', import.meta.url).href;
const { dispatchLarkEvent } = await import(REAL_DISPATCH);

describe('dispatchLarkEvent', () => {
    beforeEach(() => {
        insertEventCalls.length = 0;
        handlerCalls.length = 0;
        registered = {};
        activeContext = {};
    });

    it('找到 handler → 在 context 内调用 + 审计落库', async () => {
        registered['im.message.receive_v1'] = async (p) => {
            handlerCalls.push({ type: 'im.message.receive_v1', params: p });
        };

        await dispatchLarkEvent({
            eventType: 'im.message.receive_v1',
            params: { message: { chat_id: 'oc_1' } },
            botName: 'chiwei',
        });
        // handler 是 fire-and-forget，等一个 microtask 轮转
        await new Promise((r) => setTimeout(r, 0));

        expect(handlerCalls.length).toBe(1);
        expect(handlerCalls[0].type).toBe('im.message.receive_v1');
        expect(insertEventCalls.length).toBe(1);
    });

    it('找不到 handler → 不抛错（未知事件静默跳过）', async () => {
        await expect(
            dispatchLarkEvent({
                eventType: 'im.unknown_v1',
                params: { foo: 1 },
                botName: 'chiwei',
            }),
        ).resolves.toBeUndefined();
        await new Promise((r) => setTimeout(r, 0));
        expect(handlerCalls.length).toBe(0);
    });

    it('lane 透传进 context（跨 lane 消费侧复用）', async () => {
        let seenLane = '';
        registered['im.message.receive_v1'] = async () => {
            const { context } = await import('@middleware/context');
            seenLane = context.getLane();
        };
        await dispatchLarkEvent({
            eventType: 'im.message.receive_v1',
            params: {},
            botName: 'chiwei',
            lane: 'ppe-x',
        });
        await new Promise((r) => setTimeout(r, 0));
        expect(seenLane).toBe('ppe-x');
    });
});
