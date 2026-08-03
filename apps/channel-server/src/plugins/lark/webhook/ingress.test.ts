import { describe, it, expect, beforeEach, mock, afterAll } from 'bun:test';

// ingress 把飞书 SDK 的事件回调映射成 dispatchLarkEvent(本进程入站收口)。
// 这里只验「SDK 回调 → dispatch 契约」这层映射纯逻辑：拿到 event_type / botName
// 正确投给 dispatch、SDK 要求的同步 ack（返回 {}）。SDK 本身的 EventDispatcher /
// WSClient 接线是 glue，留给 coe e2e 验。
//
// bun 的 mock.module 是**整模块替换 + 进程级全局**，且 mock.restore() 不撤销它：
// 手写的 stub 对象会把同模块其他导出一并抹掉。本文件之前手写
// @plugins/lark/events/event-registry 的 stub 漏了 registerEventHandlerInstance，
// 而 dispatch.ts 正好 import 它 —— 单跑本文件直接 SyntaxError 挂掉。
// 现在一律「先抓真身 → 只覆盖需要的导出 → afterAll 注回去」。

type DispatchCall = { eventType: string; botName?: string; params: unknown };
const dispatchCalls: DispatchCall[] = [];
let registered: Record<string, ((params: unknown) => Promise<void>) | undefined> = {};
let activeBotName: string | undefined;

// dispatch 会 fire-and-forget 调 insertEvent 做审计落库；测试环境没有 mongo
// 连接，桩掉避免每条事件刷一行连接错误。
const realMongo = { ...(await import('@dal/mongo/client')) };
mock.module('@dal/mongo/client', () => ({
    ...realMongo,
    insertEvent: async () => undefined,
}));

// 只接管两个导出：handler 查表（换成本文件的 registered 表）和 dispatch 首次调用时
// 的自动注册（置空，避免把真实 lark handler 灌进进程级 EventRegistry 单例、
// 也避免 handler 真去连 DB）。其余导出（EventHandler 装饰器、
// getEventHandlerMetadata…）保持真身。
//
// 注意：EventRegistry 被换成裸对象后，event-registry.ts 内部对它的引用也跟着变
// （bun 换的是整个模块 namespace，模块内部的 live binding 一起换），所以真身的
// registerEventHandlerInstance 会在这里踩空 —— 必须一起接管，不能只换 EventRegistry。
const realEventRegistry = { ...(await import('@plugins/lark/events/event-registry')) };
mock.module('@plugins/lark/events/event-registry', () => ({
    ...realEventRegistry,
    EventRegistry: {
        getHandlerByEventType: (eventType: string) => registered[eventType],
    },
    registerEventHandlerInstance: () => {},
}));

// context 只覆盖三个取值口径，asyncLocalStorage 等其余导出保持真身。
const realCtx = { ...(await import('@middleware/context')) };
mock.module('@middleware/context', () => ({
    ...realCtx,
    context: {
        ...realCtx.context,
        createContext: (botName?: string) => ({ botName, traceId: 't' }),
        run: async (ctx: { botName?: string }, cb: () => Promise<unknown>) => {
            activeBotName = ctx.botName;
            return cb();
        },
        getBotName: () => activeBotName,
    },
}));

const { createLarkEventHandler, createLarkCardHandler } = await import('./ingress');

afterAll(() => {
    mock.module('@dal/mongo/client', () => realMongo);
    mock.module('@plugins/lark/events/event-registry', () => realEventRegistry);
    mock.module('@middleware/context', () => realCtx);
});

describe('createLarkEventHandler', () => {
    beforeEach(() => {
        dispatchCalls.length = 0;
        registered = {};
        activeBotName = undefined;
    });

    it('SDK 回调 → 用 params.event_type + botName 投给 dispatch，并同步返回 {}', async () => {
        registered['im.message.receive_v1'] = async (params) => {
            dispatchCalls.push({
                eventType: 'im.message.receive_v1',
                botName: activeBotName,
                params,
            });
        };
        const handler = createLarkEventHandler('chiwei');
        const ret = handler({ event_type: 'im.message.receive_v1', message: { chat_id: 'oc_1' } });
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(ret).toEqual({});
        expect(dispatchCalls.length).toBe(1);
        expect(dispatchCalls[0].eventType).toBe('im.message.receive_v1');
        expect(dispatchCalls[0].botName).toBe('chiwei');
    });

    it('params 缺 event_type → 投 unknown（不丢事件，可观测）', async () => {
        registered.unknown = async (params) => {
            dispatchCalls.push({ eventType: 'unknown', botName: activeBotName, params });
        };
        const handler = createLarkEventHandler('chiwei');
        handler({ message: {} });
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(dispatchCalls[0].eventType).toBe('unknown');
    });
});

describe('createLarkCardHandler', () => {
    beforeEach(() => {
        dispatchCalls.length = 0;
        registered = {};
        activeBotName = undefined;
    });

    it('卡片回调 → 固定投 card.action.trigger，并同步返回 {}', async () => {
        registered['card.action.trigger'] = async (params) => {
            dispatchCalls.push({
                eventType: 'card.action.trigger',
                botName: activeBotName,
                params,
            });
        };
        const handler = createLarkCardHandler('chiwei');
        const ret = handler({ action: { value: {} } });
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(ret).toEqual({});
        expect(dispatchCalls.length).toBe(1);
        expect(dispatchCalls[0].eventType).toBe('card.action.trigger');
        expect(dispatchCalls[0].botName).toBe('chiwei');
    });
});
