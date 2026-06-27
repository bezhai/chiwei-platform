import { describe, it, expect } from 'bun:test';
import type { CustomInboundMessage } from '@inner/shared/protocols';
import { createQQGatewayApp, type QQGatewayDeps } from './app';
import { PassiveWindowManager, InMemoryPassiveWindowStore } from '../passive-window/manager';
import { ed25519Sign, verifyWebhookSignature } from '../qq/webhook-verify';

const BOT_SECRET = 'bot-secret-1';
const INNER_SECRET = 'inner-secret-1';
const WEBHOOK_PATH = '/qq/webhook';
const NOOP_LOG = { info: () => {}, warn: () => {}, error: () => {} };

interface SentCall {
    kind: 'c2c' | 'group';
    target: string;
    content: string;
    msgId: string;
    msgSeq: number;
}

function build(overrides: Partial<QQGatewayDeps> = {}) {
    const forwarded: CustomInboundMessage[] = [];
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
        botSecret: BOT_SECRET,
        webhookPath: WEBHOOK_PATH,
        innerSecret: INNER_SECRET,
        windowManager: new PassiveWindowManager(new InMemoryPassiveWindowStore(), { now: () => 1_700_000_000_000 }),
        qqClient,
        forwardInbound: async (m) => {
            forwarded.push(m);
        },
        log: NOOP_LOG,
        ...overrides,
    };
    const { app, flush } = createQQGatewayApp(deps);
    return { app, flush, forwarded, sent };
}

function signedHeaders(bodyStr: string, timestamp = '1700000000') {
    const sig = ed25519Sign(BOT_SECRET, Buffer.concat([Buffer.from(timestamp, 'utf-8'), Buffer.from(bodyStr, 'utf-8')]));
    return {
        'content-type': 'application/json',
        'x-signature-timestamp': timestamp,
        'x-signature-ed25519': sig,
    };
}

describe('webhook: op:13 callback validation handshake', () => {
    it('echoes plain_token with a valid signature over event_ts + plain_token', async () => {
        const { app } = build();
        const body = JSON.stringify({ op: 13, d: { plain_token: 'PT', event_ts: '1700000111' } });
        const res = await app.request(WEBHOOK_PATH, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body,
        });
        expect(res.status).toBe(200);
        const json = (await res.json()) as { plain_token: string; signature: string };
        expect(json.plain_token).toBe('PT');
        const ok = verifyWebhookSignature({
            body: Buffer.from('PT', 'utf-8'),
            timestamp: '1700000111',
            signature: json.signature,
            botSecret: BOT_SECRET,
        });
        expect(ok).toBe(true);
    });
});

describe('webhook: op:0 event dispatch', () => {
    it('acks 200 and forwards a normalized C2C message on a valid signature', async () => {
        const { app, flush, forwarded } = build();
        const body = JSON.stringify({
            op: 0,
            t: 'C2C_MESSAGE_CREATE',
            d: { author: { user_openid: 'u1' }, content: 'hi', id: 'M1', timestamp: '2026-06-27T00:00:00Z' },
        });
        const res = await app.request(WEBHOOK_PATH, { method: 'POST', headers: signedHeaders(body), body });
        expect(res.status).toBe(200);
        await flush();
        expect(forwarded).toHaveLength(1);
        expect(forwarded[0]).toMatchObject({ chatType: 'direct', senderId: 'u1', text: 'hi', messageId: 'M1' });
    });

    it('rejects an invalid signature with 401 and does not forward', async () => {
        const { app, flush, forwarded } = build();
        const body = JSON.stringify({ op: 0, t: 'C2C_MESSAGE_CREATE', d: { author: { user_openid: 'u1' }, content: 'hi', id: 'M1' } });
        const res = await app.request(WEBHOOK_PATH, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-signature-timestamp': '1700000000', 'x-signature-ed25519': 'deadbeef' },
            body,
        });
        expect(res.status).toBe(401);
        await flush();
        expect(forwarded).toHaveLength(0);
    });

    it('does not forward non-relayed events (e.g. GROUP_ADD_ROBOT) but still acks', async () => {
        const { app, flush, forwarded } = build();
        const body = JSON.stringify({ op: 0, t: 'GROUP_ADD_ROBOT', d: { group_openid: 'g', op_member_openid: 'm' } });
        const res = await app.request(WEBHOOK_PATH, { method: 'POST', headers: signedHeaders(body), body });
        expect(res.status).toBe(200);
        await flush();
        expect(forwarded).toHaveLength(0);
    });
});

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
