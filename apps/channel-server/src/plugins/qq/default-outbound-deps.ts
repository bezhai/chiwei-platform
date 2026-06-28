// 生产用的 QqOutboundDeps：把出站发回 qq-gateway 接到真实 LaneRouter。
// 拆成独立文件，让 outbound-capabilities.ts 在单测里零 LaneRouter / 网络依赖。

import { laneRouter } from '@infrastructure/lane-router';
import type { CustomOutboundMessage, CustomOutboundResult } from '@inner/shared/protocols';
import { validateCustomOutboundResult } from '@inner/shared/protocols';
import type { QqOutboundDeps } from './outbound-capabilities';

const QQ_GATEWAY_SERVICE = process.env.QQ_GATEWAY_SERVICE || 'qq-gateway';

export const defaultQqOutboundDeps: QqOutboundDeps = {
    async postOutbound(msg: CustomOutboundMessage): Promise<CustomOutboundResult> {
        const client = laneRouter.createClient(QQ_GATEWAY_SERVICE);
        const resp = await client.post('/qq/outbound', msg, {
            headers: {
                Authorization: `Bearer ${process.env.INNER_HTTP_SECRET}`,
            },
        });
        // 过 wire 守卫：网关回执决定 sent / messageId / reason，字段名与契约对齐
        // （历史 bug：曾读 resp.data.messageId 而网关返回 id，永远 undefined）。
        return validateCustomOutboundResult((resp as { data?: unknown }).data);
    },
};
