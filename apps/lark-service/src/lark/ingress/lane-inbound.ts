// 入口三：泳道信封的 HTTP 接收端。
//
// prod 判出「这条消息属于泳道 X」之后，带 `x-ctx-lane: X` 打一次内部 HTTP，由 sidecar
// 解析目标。收到的是**别的进程已经收下并判定过的事件的重放**，跟飞书直连的那两个入口
// 有两点不一样：
//
//   不做审计落库  原始报文在它第一次进来时就已经记过了
//   不许先应答    投递方不重试，2xx 是"处理完了"的唯一凭据；先应答再异步处理等于
//                 处理失败时谁也不知道
//
// ## 两件只在这条路上成立的事
//
// **信封的 lane 可以不等于本进程的 lane，而且这是正常情形。** 泳道的 Service 不存在时
// sidecar 把请求原样打回 prod 自己，于是 prod 收到一个写着 ppe-x 的信封。这时要以信封
// 的 lane 建立上下文照常处理（下游泳道队列因此照常生效），而不是拒绝 —— 拒绝就是泳道
// 没部署时消息谁也不处理，bot 静默变砖。
//
// **「已交接」标记必须在。** 落回 prod 之后本进程仍然是 prod、绑定仍然指向那条泳道，
// 没有这个标记就会被重新判定、重新投递，而 sidecar 又会把它打回来 —— 无限自投。这条
// 拒收由 readEnvelope 承担（见 lane-envelope.ts），端点自己不再补一道。

import type { Hono } from 'hono';
import { bearerAuthMiddleware } from '@inner/shared/middleware';

import type { LarkEvent } from './lark-event';
import {
    UnclaimedEnvelope,
    larkEventOf,
    readEnvelope,
    requireClaimed,
    type InboundLaneEnvelope,
} from './lane-envelope';

/**
 * ⚠️ **跨服务契约**：投递侧（lane-handoff.ts）打的就是这个路径，两边必须逐字相同。
 * 改名只改一边的症状是投递方拿 404，而 404 会被算成投递失败 —— 消息就此没人处理。
 */
export const LANE_INBOUND_PATH = '/api/internal/lark/lane-inbound';

export interface LaneInboundEndpoint {
    /**
     * 本进程所在泳道。**不用来判断信封收不收**，只回报给投递方 —— 它与信封的 lane
     * 不等就说明这次交接落回了 prod（见文件头）。
     */
    lane: string;
    /** 本服务认领了哪些事件类型。 */
    handles(eventType: string): boolean;
    deliver(event: LarkEvent): Promise<void>;
}

export function registerLarkLaneInbound(app: Hono, endpoint: LaneInboundEndpoint): void {
    app.post(LANE_INBOUND_PATH, bearerAuthMiddleware, async (c) => {
        let envelope: InboundLaneEnvelope;
        try {
            envelope = readEnvelope(await c.req.text());
            requireClaimed(envelope, (type) => endpoint.handles(type));
        } catch (error) {
            // 投递方错了，重发一模一样的请求还是错的。说清楚是哪一条不满足，并把"报文
            // 不成立"（400）与"报文成立但送错了地方"（422）分开 —— 与 channel-server 的
            // 镜像端点同一口径。
            const status = error instanceof UnclaimedEnvelope ? 422 : 400;
            const message = (error as Error).message;
            console.warn(`[lane-inbound] refusing an envelope with ${status}: ${message}`);
            return c.json({ success: false, message }, status);
        }

        // 处理**同步**做完再应答，报错一路抛到 app.onError（500）。在这里吞掉就是投递方
        // 以为送到了、实际没人处理，而且没有任何信号。
        await endpoint.deliver(larkEventOf(envelope));

        return c.json({ success: true, handled_by_lane: endpoint.lane });
    });

    console.info(`[lane-inbound] registered at ${LANE_INBOUND_PATH} on lane=${endpoint.lane}`);
}
