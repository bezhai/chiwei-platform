import { describe, it, expect, beforeEach, mock, afterAll } from 'bun:test';

// dispatchLarkEvent 是飞书事件进入本进程入站链路的唯一收口：审计落库 +
// 在 bot context 内按 event_type 找 handler 异步执行。webhook 入口调它，
// 避免两份重复的分发逻辑。
//
// 这里只验它的分发契约（找到 handler→调用、找不到→不抛、审计被调用）：不连真实
// mongo，也不让真实 lark handler 进注册表。handlers 模块本身走真身加载（它只是
// 被 dispatch 传给注册函数，而注册函数在这里被置空）。

// bun 的 mock.module 是**整模块替换 + 进程级全局**，且 mock.restore() 不撤销它：
// 手写 stub 对象会把同模块其他导出一并抹掉，同一轮里后跑的测试文件（包括被测的
// 生产代码）就会拿到残缺模块。所以下面一律「先抓真身 → 只覆盖需要的导出 →
// afterAll 注回去」。

const insertEventCalls: unknown[] = [];
const realMongo = { ...(await import('@dal/mongo/client')) };
mock.module('@dal/mongo/client', () => ({
    ...realMongo,
    insertEvent: async (e: unknown) => {
        insertEventCalls.push(e);
    },
}));

const handlerCalls: Array<{ type: string; params: unknown }> = [];
let registered: Record<string, ((p: unknown) => Promise<void>) | undefined> = {};
type TestContext = { botName?: string; traceId?: string; lane?: string };
let activeContext: TestContext = {};

// 只接管两个导出：handler 查表（换成本文件的 registered 表）和 dispatch 首次调用
// 时的自动注册（置空，避免把真实 lark handler 灌进进程级 EventRegistry 单例）。
// 两个必须一起换：EventRegistry 被换成裸对象后，event-registry.ts 内部对它的引用
// 也跟着变（bun 换的是整个模块 namespace），真身的 registerEventHandlerInstance
// 会踩空 EventRegistry.register。
const realEventRegistry = { ...(await import('@plugins/lark/events/event-registry')) };
mock.module('@plugins/lark/events/event-registry', () => ({
    ...realEventRegistry,
    EventRegistry: {
        getHandlerByEventType: (t: string) => registered[t],
    },
    registerEventHandlerInstance: () => {},
}));

// context 只覆盖取值口径，asyncLocalStorage 等其余导出保持真身。
const realCtx = { ...(await import('@middleware/context')) };
mock.module('@middleware/context', () => ({
    ...realCtx,
    context: {
        ...realCtx.context,
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

// 防御性：dispatch 是本文件的被测对象，绝不能拿到别处装上的 stub。bun 的
// module mock 是进程级的，用绝对 file URL 强制加载真实实现。
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

afterAll(() => {
    mock.module('@dal/mongo/client', () => realMongo);
    mock.module('@plugins/lark/events/event-registry', () => realEventRegistry);
    mock.module('@middleware/context', () => realCtx);
});
