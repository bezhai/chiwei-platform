import { randomUUID } from 'crypto';
import { LaneRouter } from '@inner/shared';
import { LaneResolver } from './lane-resolver';

/**
 * 事件转发器
 * 将 Lark SDK 解析后的事件 POST 到目标 namespace 的 lark-server 统一接口
 */
export class EventForwarder {
    private secret: string;

    constructor(
        private laneResolver: LaneResolver,
        private laneRouter: LaneRouter,
    ) {
        this.secret = process.env.INNER_HTTP_SECRET || '';
        if (!this.secret) {
            console.warn('INNER_HTTP_SECRET not set, forwarding will fail auth');
        }
    }

    /**
     * 转发事件到 lark-server（fire-and-forget）
     */
    forward(eventType: string, botName: string, params: unknown): void {
        this.doForward(eventType, botName, params).catch((err) => {
            console.error(`[forwarder] failed to forward ${eventType} for ${botName}:`, err);
        });
    }

    private async doForward(eventType: string, botName: string, params: unknown): Promise<void> {
        const lane = await this.laneResolver.resolve('bot', botName);
        const url = this.laneRouter.resolveUrl('lark-server', '/api/internal/lark-event', lane || undefined);
        const traceId = randomUUID();

        console.info(
            `[forwarder] ${eventType} for ${botName} → lark-server (lane: ${lane || 'default'}, trace: ${traceId})`,
        );

        const headers: Record<string, string> = {
            'Content-Type': 'application/json',
            'X-App-Name': botName,
            'x-trace-id': traceId,
            Authorization: `Bearer ${this.secret}`,
        };
        if (lane) {
            headers['x-lane'] = lane;
        }

        const resp = await fetch(url, {
            method: 'POST',
            headers,
            body: JSON.stringify({ event_type: eventType, params }),
        });

        if (!resp.ok) {
            const body = await resp.text().catch(() => '');
            console.error(
                `[forwarder] lark-server responded ${resp.status} for ${eventType}: ${body}`,
            );
        }
    }

    /**
     * 创建通用事件 handler（SDK 回调 → return {} + 异步转发）
     */
    createHandler(botName: string): (params: unknown) => Record<string, never> {
        return (params: unknown): Record<string, never> => {
            const eventType = (params as { event_type?: string })?.event_type || 'unknown';
            console.info(`[${botName}] receive event_type: ${eventType}`);
            this.forward(eventType, botName, params);
            return {};
        };
    }

    /**
     * 创建卡片动作 handler（SDK 回调 → return {} + 异步转发）
     */
    createCardHandler(botName: string): (data: unknown) => Record<string, never> {
        return (data: unknown): Record<string, never> => {
            console.info(`[${botName}] receive card action`);
            this.forward('card.action.trigger', botName, data);
            return {};
        };
    }
}
