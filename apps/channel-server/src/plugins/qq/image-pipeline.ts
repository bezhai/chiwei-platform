// QQ 入站识图管线（对飞书 image-pipeline）。把入站图片附件喂给 tool-service 识图，
// 让赤尾在 QQ 上也能看图聊天（共享媒体轨）。
//
// fire-and-forget、best-effort：失败只记日志，绝不 gate 入站（图片本身已无条件落进
// common_message.content）。
//
// 与飞书的差异（已知 gap，待 agent-service 侧收敛）：飞书传 file_key 由 agent-service
// 经飞书 SDK 下载；QQ 附件是网关原样透传的公网 url，这里同时带上 url。tool-service
// 的 /api/image-pipeline/process 对 url 入参的支持需 agent-service 侧确认/扩展，
// 不在本插件范围（不碰 agent-service）。message_id 用全局 common message id（与
// agent-service ImageRegistry 的 key 口径一致，见 workers/image-registry-key.ts）。

import type { InboundMessage } from '@core/channels/contracts';
import { laneRouter } from '@infrastructure/lane-router';

export function enqueueQqImagePipeline(
    inbound: InboundMessage,
    commonMessageId: string,
    botName: string | undefined,
): void {
    const imageUrls = inbound.content
        .filter((c) => c.kind === 'image')
        .map((c) => (c as { key: string }).key);
    if (imageUrls.length === 0) return;

    const toolClient = laneRouter.createClient('tool-service');
    for (const url of imageUrls) {
        toolClient
            .post(
                '/api/image-pipeline/process',
                { message_id: commonMessageId, file_key: url, url },
                {
                    headers: {
                        Authorization: `Bearer ${process.env.INNER_HTTP_SECRET}`,
                        'X-App-Name': botName,
                    },
                },
            )
            .catch((err) => {
                console.error('[qq image-pipeline] error:', err);
            });
    }
}
