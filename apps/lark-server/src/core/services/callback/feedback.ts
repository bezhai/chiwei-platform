import { LarkCardThumbsDownCallback, LarkCardThumbsUpCallback } from 'types/lark';
import { ResponseFeedbackRepository } from 'infrastructure/dal/repositories/repositories';

export async function handleFeedback(
    data: LarkCardThumbsDownCallback | LarkCardThumbsUpCallback,
    union_id: string,
) {
    if (union_id !== data.union_id) {
        // 不是当前用户反馈, 不处理
        return;
    }

    await ResponseFeedbackRepository.save({
        message_id: data.message_id,
        chat_id: data.chat_id,
        parent_message_id: data.parent_message_id,
        feedback_type: data.type,
    });
}
