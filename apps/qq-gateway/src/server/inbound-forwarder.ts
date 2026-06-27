/**
 * 入站转发：把归一化后的 CustomInboundMessage 经 LaneRouter POST 给 channel-server。
 *
 * 发出前先 validateCustomInboundMessage（wire 边界守卫），Bearer 内网鉴权对齐 channel-server
 * 的 /api/internal/* 口径（INNER_HTTP_SECRET）。
 */

import { validateCustomInboundMessage, type CustomInboundMessage } from '@inner/shared/protocols';
import type { QQLogger } from '../qq/api';

/** 只用到 LaneRouter.fetch 的最小接口，便于注入测试假实现。 */
export interface InboundFetcher {
    fetch(service: string, path: string, init?: RequestInit): Promise<Response>;
}

export interface InboundForwarderDeps {
    fetcher: InboundFetcher;
    service: string;
    path: string;
    innerSecret: string;
    log: QQLogger;
}

export function createInboundForwarder(
    deps: InboundForwarderDeps,
): (msg: CustomInboundMessage) => Promise<void> {
    return async (msg: CustomInboundMessage): Promise<void> => {
        // wire 边界守卫：发出前校验，失败 fail-loud
        const validated = validateCustomInboundMessage(msg);
        const res = await deps.fetcher.fetch(deps.service, deps.path, {
            method: 'POST',
            headers: {
                Authorization: `Bearer ${deps.innerSecret}`,
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(validated),
        });
        if (!res.ok) {
            const body = await res.text().catch(() => '');
            throw new Error(`forwardInbound: channel-server responded HTTP ${res.status}: ${body.slice(0, 200)}`);
        }
        deps.log.info(`[qq-gateway] forwarded ${validated.chatType} msg ${validated.messageId} to ${deps.service}`);
    };
}
