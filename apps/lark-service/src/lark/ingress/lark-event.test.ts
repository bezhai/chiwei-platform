import { describe, expect, it } from 'bun:test';
import { context } from '@inner/shared/middleware';

import { deliverLarkEvent, type LarkEvent, type LarkEventHandlers } from './lark-event';

function anEvent(overrides: Partial<LarkEvent> = {}): LarkEvent {
    return {
        type: 'im.message.receive_v1',
        payload: { message: { message_id: 'om_1' } },
        botName: 'chiwei',
        ...overrides,
    };
}

describe('deliverLarkEvent', () => {
    it('hands the event to the handler registered for its type', async () => {
        const seen: LarkEvent[] = [];
        const handlers: LarkEventHandlers = {
            'im.message.receive_v1': async (event) => {
                seen.push(event);
            },
        };

        await deliverLarkEvent(anEvent(), handlers);
        expect(seen).toHaveLength(1);
        expect(seen[0]!.payload).toEqual({ message: { message_id: 'om_1' } });
    });

    it('picks the handler by event type', async () => {
        const called: string[] = [];
        const handlers: LarkEventHandlers = {
            'im.message.receive_v1': async () => void called.push('message'),
            'card.action.trigger': async () => void called.push('card'),
        };

        await deliverLarkEvent(anEvent({ type: 'card.action.trigger' }), handlers);
        expect(called).toEqual(['card']);
    });

    // 处理这条消息的是哪个 bot、走哪条泳道、trace 是什么，下游（bot 目录、规则、
    // 发 MQ）全靠请求上下文读。上下文不是参数、传不下去，只能在这里开。
    it('opens a request context the handler can read', async () => {
        let seen: { botName: string; lane: string; traceId: string } | undefined;
        const handlers: LarkEventHandlers = {
            'im.message.receive_v1': async () => {
                seen = {
                    botName: context.getBotName(),
                    lane: context.getLane(),
                    traceId: context.getTraceId(),
                };
            },
        };

        await deliverLarkEvent(
            anEvent({ botName: 'chiwei', lane: 'ppe-x', traceId: 'trace-1' }),
            handlers,
        );
        expect(seen).toEqual({ botName: 'chiwei', lane: 'ppe-x', traceId: 'trace-1' });
    });

    it('mints a trace id when the event carries none', async () => {
        let traceId = '';
        await deliverLarkEvent(anEvent(), {
            'im.message.receive_v1': async () => {
                traceId = context.getTraceId();
            },
        });
        expect(traceId).toMatch(/^[0-9a-f-]{36}$/);
    });

    // 没人认领的事件类型不该让入站链断掉 —— 飞书会推很多我们没订阅的东西。
    it('ignores an event type nobody handles', async () => {
        await expect(deliverLarkEvent(anEvent({ type: 'im.chat.updated_v1' }), {})).resolves
            .toBeUndefined();
    });

    // 报错必须往外抛：泳道交接的接收端靠它应答非 2xx。想要"快速 ack、异步处理"
    // 的是飞书 SDK 那两个入口，那是它们自己的事（见 event-sink.ts），不是这里的。
    it('lets a handler failure reach the caller', async () => {
        await expect(
            deliverLarkEvent(anEvent(), {
                'im.message.receive_v1': async () => {
                    throw new Error('boom');
                },
            }),
        ).rejects.toThrow('boom');
    });

    it('waits for the handler to finish', async () => {
        let finished = false;
        await deliverLarkEvent(anEvent(), {
            'im.message.receive_v1': async () => {
                await Bun.sleep(5);
                finished = true;
            },
        });
        expect(finished).toBe(true);
    });
});
