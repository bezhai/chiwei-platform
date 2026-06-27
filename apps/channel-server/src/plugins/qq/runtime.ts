// QQ ChannelRuntime（对飞书 plugins/lark/runtime.ts）。
//
// 入站入口是内网 HTTP：qq-gateway 把归一化好的 CustomInboundMessage POST 到
// POST /api/internal/qq/inbound（内网 Bearer 鉴权，与现有 /api/internal/* 一致）。
// 没有飞书那种 SDK 长连 / webhook 握手；验签 / 握手都在网关侧做完。

import type { Hono } from 'hono';
import type { BotConfig } from '@entities/bot-config';
import type { InboundLaneEnvelope } from '@integrations/inbound-lane';
import type { ChannelRuntime } from '@plugins/runtime';
import { bearerAuthMiddleware } from '@inner/shared/middleware';
import { validateCustomInboundMessage, type CustomInboundMessage } from '@inner/shared/protocols';
import { context } from '@middleware/context';
import { qqEventHandlers } from './events/handlers';

const QQ_INBOUND_PATH = '/api/internal/qq/inbound';

export const qqRuntime: ChannelRuntime = {
    channel: 'qq',

    registerHttpIngress(app: Hono, bots: BotConfig[]): void {
        app.post(QQ_INBOUND_PATH, bearerAuthMiddleware, async (c) => {
            let msg: CustomInboundMessage;
            try {
                const body = await c.req.json();
                msg = validateCustomInboundMessage(body);
            } catch (err) {
                console.warn(`[qq ingress] invalid CustomInboundMessage: ${(err as Error).message}`);
                return c.json({ success: false, message: (err as Error).message }, 400);
            }
            // botName 来自 payload（内网投递不靠 header），注入 context 供入站处理读取。
            await context.run(
                context.createContext(msg.botName, context.getTraceId(), context.getLane() || undefined),
                async () => {
                    await qqEventHandlers.handleInbound(msg);
                },
            );
            return c.json({ success: true });
        });
        console.info(
            `[ingress] qq inbound registered at ${QQ_INBOUND_PATH} (${bots.length} qq bot(s))`,
        );
    },

    // 泳道分流：prod 入口算出非本进程 lane 时把信封投到 inbound_lane.{lane}，
    // 目标 lane channel-server 消费后在此重走入站后半段（consumer 已注入 context）。
    async handleInboundLaneEnvelope(env: InboundLaneEnvelope): Promise<void> {
        await qqEventHandlers.handleInbound(env.params as CustomInboundMessage);
    },
};
