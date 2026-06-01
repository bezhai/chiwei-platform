import { describe, it, expect, beforeEach, mock } from 'bun:test';

// ingress 把飞书 SDK 的事件回调映射成 dispatchLarkEvent(本进程入站收口)。
// 这里只验「SDK 回调 → dispatch 契约」这层映射纯逻辑：拿到 event_type / botName
// 正确投给 dispatch、SDK 要求的同步 ack（返回 {}）。SDK 本身的 EventDispatcher /
// WSClient 接线是 glue，留给 coe e2e 验。

type DispatchCall = { eventType: string; botName?: string; params: unknown };
const dispatchCalls: DispatchCall[] = [];
let registered: Record<string, ((params: unknown) => Promise<void>) | undefined> = {};
let activeBotName: string | undefined;

mock.module('@dal/mongo/client', () => ({
    insertEvent: async () => undefined,
}));

mock.module('@lark/events/event-registry', () => ({
    EventHandler: () => () => undefined,
    EventRegistry: {
        getHandlerByEventType: (eventType: string) => registered[eventType],
    },
}));
mock.module('@lark/events/handlers', () => ({
    larkEventHandlers: {},
}));
mock.module('@middleware/context', () => ({
    context: {
        createContext: (botName?: string) => ({ botName, traceId: 't' }),
        run: async (ctx: { botName?: string }, cb: () => Promise<unknown>) => {
            activeBotName = ctx.botName;
            return cb();
        },
        getBotName: () => activeBotName,
    },
}));

const { createLarkEventHandler, createLarkCardHandler } = await import('./ingress');

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
