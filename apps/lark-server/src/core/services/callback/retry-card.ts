import { LarkCardRetryCallback } from 'types/lark';
import { reCreateCard } from '@core/services/ai/reply';

export async function handleRetryCard(data: LarkCardRetryCallback) {
    reCreateCard(data.message_id, data.parent_message_id, data.chat_id, data.root_id, data.is_p2p);
}
