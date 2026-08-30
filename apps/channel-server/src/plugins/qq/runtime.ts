// QQ ChannelRuntime：本服务当前唯一的 runtime。
//
// 两条内网入站端点，两份契约，所以是两条路由而不是一条端点两种报文：
//
//   POST /api/internal/qq/inbound       qq-gateway 归一化好的 CustomInboundMessage
//   POST /api/internal/qq/lane-inbound  prod 判过泳道之后交接来的信封（见 lane-inbound.ts）
//
// 两条都在内网 Bearer 之后。验签 / 握手都在网关侧做完，所以这里不需要飞书那种 webhook
// 握手或 SDK 长连（飞书那套现在在 apps/lark-service/src/lark/ingress/ 里，不是一个进程了）。

import type { Hono } from 'hono';
import type { BotConfig } from '@inner/shared/entities';
import type { ChannelRuntime } from '@plugins/runtime';
import { bearerAuthMiddleware } from '@inner/shared/middleware';
import { getLane } from '@inner/shared/mq';
import { validateCustomInboundMessage, type CustomInboundMessage } from '@inner/shared/protocols';
import { context } from '@middleware/context';
import { qqEventHandlers } from './events/handlers';
import { registerQqLaneInbound } from './lane-inbound';

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
            try {
                await context.run(
                    context.createContext(msg.botName, context.getTraceId(), context.getLane() || undefined),
                    async () => {
                        await qqEventHandlers.handleInbound(msg);
                    },
                );
            } catch (err) {
                // 处理失败要让 qq-gateway 知道。它不会重投，但把失败留在自己的日志里比
                // 这边回 200、两边都当成功要好查得多。
                console.error(
                    `[qq ingress] inbound handling failed: ` +
                        `qq_message_id=${msg.messageId} detail=${(err as Error).message}`,
                );
                return c.json({ success: false, message: (err as Error).message }, 500);
            }
            return c.json({ success: true });
        });

        // 泳道信封入口：prod 判出非本进程 lane 时把信封 POST 到这里（目标由 sidecar 按
        // x-ctx-lane 解析，泳道不存在时打回 prod 自己）。
        registerQqLaneInbound(app, {
            handle: (message) => qqEventHandlers.handleInbound(message, { handedOff: true }),
            processLane: () => getLane() ?? 'prod',
        });

        console.info(
            `[ingress] qq inbound registered at ${QQ_INBOUND_PATH} (${bots.length} qq bot(s))`,
        );
    },
};
