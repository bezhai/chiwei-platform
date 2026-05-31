import { describe, it, expect, beforeEach, mock } from 'bun:test';

const dispatchCalls: Array<{
    eventType: string;
    params: unknown;
    botName?: string;
    traceId?: string;
    lane?: string;
}> = [];
mock.module('@plugins/lark/webhook/dispatch', () => ({
    dispatchLarkEvent: async (input: {
        eventType: string;
        params: unknown;
        botName?: string;
        traceId?: string;
        lane?: string;
    }) => {
        dispatchCalls.push(input);
        const { insertEvent } = await import('@dal/mongo/client');
        const { context } = await import('@middleware/context');
        const { EventRegistry, registerEventHandlerInstance } = await import(
            '@lark/events/event-registry'
        );

        insertEvent(input.params as Record<string, unknown>).catch(() => {});
        registerEventHandlerInstance({});
        const ctx = context.createContext(input.botName, input.traceId, input.lane);
        await context.run(ctx, async () => {
            const handler = EventRegistry.getHandlerByEventType(input.eventType);
            if (handler) {
                handler(input.params).catch(() => {});
            }
        });
    },
}));

const { default: app } = await import('./internal-lark.route');

describe('internal lark route', () => {
    beforeEach(() => {
        process.env.INNER_HTTP_SECRET = 'inner-secret';
        dispatchCalls.length = 0;
    });

    it('uses dispatchLarkEvent as the single dispatch entry and forwards context headers', async () => {
        const params = { message: { message_id: 'm1' } };

        const res = await app.request('/api/internal/lark-event', {
            method: 'POST',
            headers: {
                Authorization: 'Bearer inner-secret',
                'Content-Type': 'application/json',
                'X-App-Name': 'chiwei',
                'x-trace-id': 'trace-1',
                'x-ctx-lane': 'ppe-foo',
            },
            body: JSON.stringify({
                event_type: 'im.message.receive_v1',
                params,
            }),
        });

        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({ ok: true });
        expect(dispatchCalls).toEqual([
            {
                eventType: 'im.message.receive_v1',
                params,
                botName: 'chiwei',
                traceId: 'trace-1',
                lane: 'ppe-foo',
            },
        ]);
    });
});
