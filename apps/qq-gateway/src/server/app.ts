/**
 * QQ 网关 HTTP 应用（hono）。入站走 WebSocket 主动长连接（见 qq/gateway-client.ts），不在 HTTP 层；
 * 本 app 只剩出站一条路由：
 *
 *  POST /qq/outbound   — 收 channel-server 的 CustomOutboundMessage（Bearer 内网鉴权）
 *      validate → 被动窗口 reserve → 调 QQ api 发文本；丢弃情形 200 返回 {sent:false, reason}
 */

import { Hono } from 'hono';
import { createBearerAuthMiddleware } from '@inner/shared/middleware';
import { validateCustomOutboundMessage, type CustomOutboundResult } from '@inner/shared/protocols';
import type { PassiveWindowManager } from '../passive-window/manager';
import type { QQLogger, SendOptions, SendResult } from '../qq/api';

/** 网关只用到 QQClient 的这两个发送方法，按接口注入便于测试。 */
export interface QQSender {
    sendC2CMessage(openid: string, content: string, opts: SendOptions): Promise<SendResult>;
    sendGroupMessage(groupOpenid: string, content: string, opts: SendOptions): Promise<SendResult>;
}

export interface QQGatewayDeps {
    botName: string;
    innerSecret: string;
    windowManager: PassiveWindowManager;
    qqClient: QQSender;
    log: QQLogger;
}

export function createQQGatewayApp(deps: QQGatewayDeps): { app: Hono } {
    const app = new Hono();

    app.get('/health', (c) => c.json({ ok: true, service: 'qq-gateway', bot: deps.botName }));

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

    return { app };
}
