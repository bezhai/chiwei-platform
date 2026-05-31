// recall-worker 撤回派发。逐条撤回 agent_responses.replies[].message_id（现状是
// 渠道裸 message id），从 worker inline 的飞书 deleteMessage 改成走能力端口
// capabilities.recall。撤回循环、成功/失败计数是 worker 编排，留在这里；端口只
// 做「撤回这一条」的原子飞书 SDK 调用，worker 不再 import 飞书 SDK。
//
// 单条失败不中断后续（与现状 try/catch per reply 一致）：一条删不掉不该让其余
// 回复留着不删。最终 recalled/failed 计数由 worker 决定标 recalled 还是 recall_failed。

import type { OutboundCapabilities } from '@core/ports/channel-plugin';

export interface RecallReplyRef {
    message_id: string; // 渠道裸 message id（现状 replies 里存的就是飞书裸 id）
}

export interface RecallResult {
    recalled: number;
    failed: number;
}

export async function recallReplies(
    cap: OutboundCapabilities,
    replies: RecallReplyRef[],
): Promise<RecallResult> {
    // 平台不支持撤回（端口无 recall）= fail-loud：撤回请求落到没有撤回能力的
    // channel 是装配错误，绝不静默吞掉。
    if (!cap.recall) {
        throw new Error(
            'recall requested but channel capability has no recall(); ' +
                'platform does not support recall — fail-loud, no silent skip',
        );
    }

    let recalled = 0;
    let failed = 0;
    for (const reply of replies) {
        try {
            await cap.recall({ channelId: reply.message_id });
            recalled++;
            console.info(`[RecallWorker] Recalled message: ${reply.message_id}`);
        } catch (e) {
            failed++;
            console.error(`[RecallWorker] Failed to recall message: ${reply.message_id}`, e);
        }
    }
    return { recalled, failed };
}
