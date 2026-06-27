/**
 * QQ 网关 HTTP 应用（hono）。两条路由：
 *
 *  1. POST {webhookPath}  — QQ webhook 入口
 *       op:13 → 用 botSecret 签 `event_ts + plain_token`，回 {plain_token, signature}（握手，无需验签）
 *       op:0  → 验 Ed25519 签名（`timestamp + body`），通过则立即 200 ack，异步归一化+转发给 channel-server
 *  2. POST /qq/outbound   — 收 channel-server 的 CustomOutboundMessage（Bearer 内网鉴权）
 *       validate → 被动窗口 reserve → 调 QQ api 发文本；丢弃情形 200 返回 {sent:false, reason}
 *
 * webhook 收发流程移植自 openclaw-qqbot/src/transport/webhook-transport.ts，去掉 openclaw plugin-sdk，
 * 改写为 hono 路由。op:13 先于验签处理、op:0 立即 ack 后异步分发，均与原实现一致。
 */

import { Hono } from 'hono';
import { createBearerAuthMiddleware } from '@inner/shared/middleware';
import {
    validateCustomOutboundMessage,
    type CustomInboundMessage,
    type CustomOutboundResult,
} from '@inner/shared/protocols';
import { verifyWebhookSignature, signValidationResponse } from '../qq/webhook-verify';
import { normalizeQQEvent } from '../qq/normalize';
import type { PassiveWindowManager } from '../passive-window/manager';
import type { QQLogger, SendOptions, SendResult } from '../qq/api';

const OP_DISPATCH = 0;
const OP_VALIDATION = 13;
const OP_HTTP_CALLBACK_ACK = 12;

/** 网关只用到 QQClient 的这两个发送方法，按接口注入便于测试。 */
export interface QQSender {
    sendC2CMessage(openid: string, content: string, opts: SendOptions): Promise<SendResult>;
    sendGroupMessage(groupOpenid: string, content: string, opts: SendOptions): Promise<SendResult>;
}

export interface QQGatewayDeps {
    botName: string;
    botSecret: string;
    webhookPath: string;
    innerSecret: string;
    windowManager: PassiveWindowManager;
    qqClient: QQSender;
    /** 把归一化后的入站消息推给 channel-server。 */
    forwardInbound: (msg: CustomInboundMessage) => Promise<void>;
    log: QQLogger;
}

export function createQQGatewayApp(deps: QQGatewayDeps): { app: Hono; flush: () => Promise<void> } {
    const app = new Hono();
    const pending = new Set<Promise<void>>();

    const track = (p: Promise<void>): void => {
        pending.add(p);
        void p.finally(() => pending.delete(p));
    };

    app.get('/health', (c) => c.json({ ok: true, service: 'qq-gateway', bot: deps.botName }));

    // ── webhook 入口 ──
    app.post(deps.webhookPath, async (c) => {
        const rawBody = await c.req.text();
        let payload: { op?: number; t?: string; d?: unknown; s?: number };
        try {
            payload = JSON.parse(rawBody);
        } catch {
            deps.log.error(`[qq-gateway] webhook body not JSON: ${rawBody.slice(0, 200)}`);
            return c.json({ error: 'invalid json' }, 400);
        }

        // op:13 回调地址校验（先于验签）
        if (payload.op === OP_VALIDATION) {
            const d = payload.d as { plain_token?: string; event_ts?: string } | undefined;
            if (!d?.plain_token || !d?.event_ts) {
                return c.json({ error: 'invalid validation payload' }, 400);
            }
            return c.json(
                signValidationResponse({ plainToken: d.plain_token, eventTs: d.event_ts, botSecret: deps.botSecret }),
            );
        }

        // 验签
        const timestamp = c.req.header('x-signature-timestamp') ?? '';
        const signature = c.req.header('x-signature-ed25519') ?? '';
        if (!timestamp || !signature) {
            return c.json({ error: 'missing signature headers' }, 401);
        }
        const valid = verifyWebhookSignature({
            body: Buffer.from(rawBody, 'utf-8'),
            timestamp,
            signature,
            botSecret: deps.botSecret,
        });
        if (!valid) {
            deps.log.warn(`[qq-gateway] webhook signature verification failed`);
            return c.json({ error: 'invalid signature' }, 401);
        }

        // op:0 立即 ack，异步分发
        if (payload.op === OP_DISPATCH) {
            track(dispatchEvent(deps, payload.t ?? '', payload.d));
        }
        return c.json({ op: OP_HTTP_CALLBACK_ACK, d: 0 });
    });

    // ── 出站入口（内网 Bearer 鉴权）──
    app.use('/qq/outbound', createBearerAuthMiddleware({ getExpectedToken: () => deps.innerSecret }));
    app.post('/qq/outbound', async (c) => {
        let parsed: unknown;
        try {
            parsed = await c.req.json();
        } catch {
            return c.json({ error: 'invalid json' }, 400);
        }
        let msg;
        try {
            msg = validateCustomOutboundMessage(parsed);
        } catch (err) {
            return c.json({ error: err instanceof Error ? err.message : 'invalid outbound message' }, 400);
        }

        const text = msg.text?.trim();
        if (!text) {
            // 本期只发文本：无文本无可发（媒体后置）
            deps.log.warn(`[qq-gateway] outbound has no text, dropping (idem=${msg.idempotencyKey})`);
            return c.json({ sent: false, reason: 'empty_text' } satisfies CustomOutboundResult);
        }

        const reservation = await deps.windowManager.reserve({
            botName: msg.botName,
            replyToMessageId: msg.replyToMessageId,
            idempotencyKey: msg.idempotencyKey,
        });

        if (reservation.action === 'drop') {
            // KNOWN RESIDUAL（标注不修）：reason='duplicate' 时这条 sent:false 会被 channel-server
            // 的 postOrThrow 当成发送失败抛错，进而被 worker 标成 failed——但 duplicate 恰恰意味着
            // 同一段早已发出去过（MQ 重投 / 崩溃重投），即「其实已送达却被误标失败」。发生概率低且
            // 消息已送达，本期先留作残留项，待后续区分 duplicate-as-success 再处理。
            deps.log.warn(
                `[qq-gateway] outbound dropped reason=${reservation.reason} bot=${msg.botName} reply=${msg.replyToMessageId ?? '<none>'} idem=${msg.idempotencyKey}`,
            );
            return c.json({ sent: false, reason: reservation.reason } satisfies CustomOutboundResult);
        }

        const sendOpts: SendOptions = { msgId: msg.replyToMessageId!, msgSeq: reservation.msgSeq };
        try {
            const result =
                msg.chatType === 'group'
                    ? await deps.qqClient.sendGroupMessage(msg.conversationId, text, sendOpts)
                    : await deps.qqClient.sendC2CMessage(msg.conversationId, text, sendOpts);
            // 回执契约：字段名 messageId（不是 id），与 CustomOutboundResult / channel-server 对齐。
            return c.json({ sent: true, messageId: result.id } satisfies CustomOutboundResult);
        } catch (err) {
            // 发送失败不重试：seq 已消耗、幂等已记，重投会被去重。仅记错误并上报。
            deps.log.error(`[qq-gateway] QQ send failed idem=${msg.idempotencyKey}: ${err instanceof Error ? err.message : String(err)}`);
            return c.json({ sent: false, reason: 'send_error' } satisfies CustomOutboundResult, 502);
        }
    });

    const flush = async (): Promise<void> => {
        await Promise.allSettled([...pending]);
    };

    return { app, flush };
}

async function dispatchEvent(deps: QQGatewayDeps, eventType: string, d: unknown): Promise<void> {
    try {
        const msg = normalizeQQEvent(eventType, d, { botName: deps.botName });
        if (!msg) return; // 系统事件 / 未支持类型，不转发
        await deps.forwardInbound(msg);
    } catch (err) {
        deps.log.error(`[qq-gateway] dispatch ${eventType} failed: ${err instanceof Error ? err.message : String(err)}`);
    }
}
