/**
 * 入站转发：把归一化后的 CustomInboundMessage 经 LaneRouter POST 给 channel-server。
 *
 * 发出前先 validateCustomInboundMessage（wire 边界守卫），Bearer 内网鉴权对齐 channel-server
 * 的 /api/internal/* 口径（INNER_HTTP_SECRET）。
 */

import { validateCustomInboundMessage, type CustomInboundMessage } from '@inner/shared/protocols';
import { context } from '@inner/shared/middleware';
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
    /**
     * 本服务自身的泳道（PaaS 注入的 process.env.LANE）。WebSocket 事件回调里 forward 无入站
     * HTTP 请求、context 为空，需手动把自身 lane 放进 context，LaneRouter 才会注入 x-ctx-lane，
     * sidecar 据此路由到同 lane 的 channel-server。为空（prod）时不注入、fallback prod。
     */
    selfLane?: string;
}

export function createInboundForwarder(
    deps: InboundForwarderDeps,
): (msg: CustomInboundMessage) => Promise<void> {
    return async (msg: CustomInboundMessage): Promise<void> => {
        // wire 边界守卫：发出前校验，失败 fail-loud
        const validated = validateCustomInboundMessage(msg);
        // 在自身 lane 的 context 内发出，LaneRouter.fetch 才能注入 x-ctx-lane。
        // selfLane 为空时 lane=undefined → 不注入 → fallback prod（prod 场景的正确行为）。
        const ctx = context.createContext(undefined, {
            lane: deps.selfLane,
            botName: validated.botName,
        });
        const res = await context.run(ctx, () =>
            deps.fetcher.fetch(deps.service, deps.path, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${deps.innerSecret}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(validated),
            }),
        );
        if (!res.ok) {
            const body = await res.text().catch(() => '');
            throw new Error(`forwardInbound: channel-server responded HTTP ${res.status}: ${body.slice(0, 200)}`);
        }
        deps.log.info(`[qq-gateway] forwarded ${validated.chatType} msg ${validated.messageId} to ${deps.service}`);
    };
}
