import { describe, it, expect, beforeEach, mock } from 'bun:test';

// ingress 把飞书 SDK 的事件回调映射成 dispatchLarkEvent(本进程入站收口)。
// 这里只验「SDK 回调 → dispatch 契约」这层映射纯逻辑：拿到 event_type / botName
// 正确投给 dispatch、SDK 要求的同步 ack（返回 {}）。SDK 本身的 EventDispatcher /
// WSClient 接线是 glue，留给 coe e2e 验。

const dispatchCalls: Array<{ eventType: string; botName?: string; params: unknown }> = [];
mock.module('./dispatch', () => ({
    dispatchLarkEvent: async (input: { eventType: string; botName?: string; params: unknown }) => {
        dispatchCalls.push(input);
    },
}));

const { createLarkEventHandler, createLarkCardHandler } = await import('./ingress');

describe('createLarkEventHandler', () => {
    beforeEach(() => {
        dispatchCalls.length = 0;
    });

    it('SDK 回调 → 用 params.event_type + botName 投给 dispatch，并同步返回 {}', () => {
        const handler = createLarkEventHandler('chiwei');
        const ret = handler({ event_type: 'im.message.receive_v1', message: { chat_id: 'oc_1' } });

        expect(ret).toEqual({});
        expect(dispatchCalls.length).toBe(1);
        expect(dispatchCalls[0].eventType).toBe('im.message.receive_v1');
        expect(dispatchCalls[0].botName).toBe('chiwei');
    });

    it('params 缺 event_type → 投 unknown（不丢事件，可观测）', () => {
        const handler = createLarkEventHandler('chiwei');
        handler({ message: {} });
        expect(dispatchCalls[0].eventType).toBe('unknown');
    });
});

describe('createLarkCardHandler', () => {
    beforeEach(() => {
        dispatchCalls.length = 0;
    });

    it('卡片回调 → 固定投 card.action.trigger，并同步返回 {}', () => {
        const handler = createLarkCardHandler('chiwei');
        const ret = handler({ action: { value: {} } });

        expect(ret).toEqual({});
        expect(dispatchCalls.length).toBe(1);
        expect(dispatchCalls[0].eventType).toBe('card.action.trigger');
        expect(dispatchCalls[0].botName).toBe('chiwei');
    });
});
