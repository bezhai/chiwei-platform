import { describe, it, expect } from 'bun:test';
import { createQQGatewayApp, type QQGatewayDeps } from './app';
import { PassiveWindowManager, InMemoryPassiveWindowStore } from '../passive-window/manager';

const INNER_SECRET = 'inner-secret-1';
const NOOP_LOG = { info: () => {}, warn: () => {}, error: () => {} };

interface SentCall {
    kind: 'c2c' | 'group';
    target: string;
    content: string;
    msgId: string;
    msgSeq: number;
}

function build(overrides: Partial<QQGatewayDeps> = {}) {
    const sent: SentCall[] = [];
    const qqClient = {
        sendC2CMessage: async (openid: string, content: string, opts: { msgId: string; msgSeq: number }) => {
            sent.push({ kind: 'c2c', target: openid, content, ...opts });
            return { id: 'sent-c2c' };
        },
        sendGroupMessage: async (group: string, content: string, opts: { msgId: string; msgSeq: number }) => {
            sent.push({ kind: 'group', target: group, content, ...opts });
            return { id: 'sent-group' };
        },
    };
    const deps: QQGatewayDeps = {
        botName: 'chiwei',
        innerSecret: INNER_SECRET,
        windowManager: new PassiveWindowManager(new InMemoryPassiveWindowStore(), { now: () => 1_700_000_000_000 }),
        qqClient,
        log: NOOP_LOG,
        ...overrides,
    };
    const { app } = createQQGatewayApp(deps);
    return { app, sent };
}

describe('outbound: /qq/outbound passive reply', () => {
    function outboundReq(app: ReturnType<typeof build>['app'], body: unknown, auth = `Bearer ${INNER_SECRET}`) {
        return app.request('/qq/outbound', {
            method: 'POST',
            headers: { 'content-type': 'application/json', authorization: auth },
            body: JSON.stringify(body),
        });
    }

    it('sends a C2C passive reply with allocated msg_seq', async () => {
        const { app, sent } = build();
        const res = await outboundReq(app, {
            botName: 'chiwei',
            chatType: 'direct',
            conversationId: 'u1',
            replyToMessageId: 'M1',
            text: '回复你',
            idempotencyKey: 'idem-1',
        });
        expect(res.status).toBe(200);
        expect(sent).toHaveLength(1);
        expect(sent[0]).toEqual({ kind: 'c2c', target: 'u1', content: '回复你', msgId: 'M1', msgSeq: 1 });
        // 回执契约：成功返回 {sent:true, messageId: QQ 返回的新 msg_id}（字段名 messageId，
        // channel-server 据此落库、续段锚点）。
        const json = (await res.json()) as { sent: boolean; messageId?: string };
        expect(json.sent).toBe(true);
        expect(json.messageId).toBe('sent-c2c');
    });

    it('routes group chatType to sendGroupMessage', async () => {
        const { app, sent } = build();
        await outboundReq(app, {
            botName: 'chiwei',
            chatType: 'group',
            conversationId: 'g7',
            replyToMessageId: 'GM1',
            text: 'hi group',
            idempotencyKey: 'idem-g',
        });
        expect(sent[0]).toMatchObject({ kind: 'group', target: 'g7', msgSeq: 1 });
    });

    it('drops an active send (no replyToMessageId) without calling the QQ api', async () => {
        const { app, sent } = build();
        const res = await outboundReq(app, {
            botName: 'chiwei',
            chatType: 'direct',
            conversationId: 'u1',
            text: 'proactive',
            idempotencyKey: 'idem-active',
        });
        expect(res.status).toBe(200);
        const json = (await res.json()) as { sent: boolean; reason?: string };
        expect(json.sent).toBe(false);
        expect(json.reason).toBe('active_send');
        expect(sent).toHaveLength(0);
    });

    it('dedups a redelivered idempotencyKey (no second send)', async () => {
        const { app, sent } = build();
        const msg = {
            botName: 'chiwei',
            chatType: 'direct',
            conversationId: 'u1',
            replyToMessageId: 'M9',
            text: 'once',
            idempotencyKey: 'idem-dup',
        };
        await outboundReq(app, msg);
        const res2 = await outboundReq(app, msg);
        expect(res2.status).toBe(200);
        expect(sent).toHaveLength(1);
    });

    it('rejects missing/invalid auth with 401', async () => {
        const { app, sent } = build();
        const res = await outboundReq(
            app,
            { botName: 'chiwei', chatType: 'direct', conversationId: 'u1', replyToMessageId: 'M', text: 'x', idempotencyKey: 'k' },
            'Bearer wrong',
        );
        expect(res.status).toBe(401);
        expect(sent).toHaveLength(0);
    });

    it('rejects a malformed outbound payload with 400', async () => {
        const { app } = build();
        const res = await outboundReq(app, { botName: 'chiwei' }); // missing required fields
        expect(res.status).toBe(400);
    });
});
