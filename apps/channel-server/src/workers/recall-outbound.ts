// recall-worker 撤回派发。逐条读取 common_agent_response.replies[].common_message_id，
// 经当前 channel 插件反查渠道裸 message id，再走能力端口 capabilities.recall。
// common 消费方不接触任何平台私有映射表。
//
// 单条失败不中断后续（与现状 try/catch per reply 一致）：一条删不掉不该让其余
// 回复留着不删。最终 recalled/failed 计数由 worker 决定标 recalled 还是 recall_failed。

import type { OutboundCapabilities } from '@core/ports/channel-plugin';

export interface RecallReplyRef {
    common_message_id: string;
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
            const msgRef = await cap.resolveMessageRef({
                commonMessageId: reply.common_message_id,
            });
            await cap.recall(msgRef);
            recalled++;
            console.info(`[RecallWorker] Recalled message: ${reply.common_message_id}`);
        } catch (e) {
            failed++;
            console.error(
                `[RecallWorker] Failed to recall message: ${reply.common_message_id}`,
                e,
            );
        }
    }
    return { recalled, failed };
}
