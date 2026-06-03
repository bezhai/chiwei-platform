import { ContentType } from '@core/models/message-content';
import type { Message } from '@core/models/message';

export function renderLarkMentionText(message: Message): string {
    return message
        .contentItems()
        .map((item) => {
            if (item.type !== ContentType.Mention) {
                return item.value;
            }
            const channelUserId = item.meta?.channel_user_id;
            if (typeof channelUserId === 'string' && channelUserId.length > 0) {
                return `<at user_id="${channelUserId}"></at>`;
            }
            return `@${item.value}`;
        })
        .join('');
}
