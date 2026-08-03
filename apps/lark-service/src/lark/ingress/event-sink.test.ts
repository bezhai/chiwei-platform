import { describe, expect, it } from 'bun:test';

import { createLarkEventSink, type LarkEventSinkPorts } from './event-sink';
import type { LarkEvent } from './lark-event';

function ports(overrides: Partial<LarkEventSinkPorts> = {}) {
    const delivered: LarkEvent[] = [];
    const recorded: unknown[] = [];
    const base: LarkEventSinkPorts = {
        botName: 'chiwei',
        record: async (payload) => void recorded.push(payload),
        deliver: async (event) => void delivered.push(event),
        ...overrides,
    };
    return { ports: base, delivered, recorded };
}

const settle = () => Bun.sleep(1);

describe('createLarkEventSink', () => {
    // 飞书要求 webhook / 长连快速应答，慢一点就当我们没收到、重推。所以应答不能等
    // 处理跑完 —— 这也意味着**平台重试兜不住我们这边的失败**，事件一进来就已经算
    // 送达了。判据：处理永远不结束时，应答照样立刻返回。
    it('answers Lark without waiting for the event to be processed', () => {
        let finished = false;
        const { ports: p } = ports({
            deliver: () =>
                Bun.sleep(50).then(() => {
                    finished = true;
                }),
        });

        expect(createLarkEventSink(p).onEvent({ event_type: 'im.message.receive_v1' })).toEqual({});
        expect(finished).toBe(false);
    });

    it('delivers the event under the type Lark stamped on it', async () => {
        const { ports: p, delivered } = ports();
        const payload = { event_type: 'im.message.receive_v1', message: { message_id: 'om_1' } };

        createLarkEventSink(p).onEvent(payload);
        await settle();

        expect(delivered).toEqual([
            { type: 'im.message.receive_v1', payload, botName: 'chiwei' },
        ]);
    });

    it('still delivers an event Lark sent without a type, naming it unknown', async () => {
        const { ports: p, delivered } = ports();
        createLarkEventSink(p).onEvent({ message: {} });
        await settle();
        expect(delivered[0]!.type).toBe('unknown');
    });

    // 卡片回调走的是另一条 SDK 回调，报文里没有 event_type，类型由入口本身决定。
    it('stamps a card action with its own type', async () => {
        const { ports: p, delivered } = ports();
        const payload = { action: { value: { type: 'update-photo-card' } } };

        expect(createLarkEventSink(p).onCardAction(payload)).toEqual({});
        await settle();

        expect(delivered).toEqual([
            { type: 'card.action.trigger', payload, botName: 'chiwei' },
        ]);
    });

    it('records the raw payload for audit', async () => {
        const { ports: p, recorded } = ports();
        const payload = { event_type: 'im.message.receive_v1', message: { message_id: 'om_1' } };

        createLarkEventSink(p).onEvent(payload);
        await settle();

        expect(recorded).toEqual([payload]);
    });

    it('records card actions too', async () => {
        const { ports: p, recorded } = ports();
        createLarkEventSink(p).onCardAction({ action: {} });
        await settle();
        expect(recorded).toHaveLength(1);
    });

    // 审计是旁路。审计库挂了不该让飞书消息处理不了 —— 那是把可观测性的故障放大成
    // 业务故障。
    it('processes the event even when the audit log is down', async () => {
        const { ports: p, delivered } = ports({
            record: async () => {
                throw new Error('mongo is down');
            },
        });

        createLarkEventSink(p).onEvent({ event_type: 'im.message.receive_v1' });
        await settle();

        expect(delivered).toHaveLength(1);
    });

    // 应答已经发出去了，这时候再抛错只会变成 unhandled rejection 打死进程。
    it('does not let a processing failure escape into the SDK callback', async () => {
        const { ports: p } = ports({
            deliver: async () => {
                throw new Error('boom');
            },
        });
        const sink = createLarkEventSink(p);

        expect(() => sink.onEvent({ event_type: 'im.message.receive_v1' })).not.toThrow();
        await settle();
    });
});
