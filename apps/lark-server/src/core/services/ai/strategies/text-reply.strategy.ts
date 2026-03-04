import { replyPost, sendPost } from '@lark/basic/message';
import { markdownToPostContent } from 'core/services/message/post-content-processor';
import { MultiMessageConfig } from '@config/multi-message.config';

/**
 * 休眠函数
 */
function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface TextReplyContext {
    messageId: string;
    chatId: string;
    isP2P: boolean;
    rootId?: string;
}

/**
 * Text 回复策略
 * 用于 RabbitMQ 队列模式：收到完整内容后一次性发送 post 消息
 */
export class TextReplyStrategy {
    constructor(
        private context: TextReplyContext,
        private config: MultiMessageConfig,
    ) {}

    /**
     * 处理完整回复内容并发送消息
     * 返回第一条消息的 message_id（如果能获取到的话，目前 replyPost/sendPost 不返回 message_id）
     */
    async sendReply(content: string): Promise<void> {
        const { splitMarker, maxMessages, defaultDelay, minDelay, maxDelay } = this.config;

        // 按分隔符拆分内容
        const parts = content
            .split(splitMarker)
            .map((p) => p.trim())
            .filter(Boolean);

        if (parts.length === 0) {
            return;
        }

        // 限制消息数量，超出部分合并到最后一条
        let messages: string[];
        if (parts.length > maxMessages) {
            messages = parts.slice(0, maxMessages - 1);
            messages.push(parts.slice(maxMessages - 1).join('\n\n'));
        } else {
            messages = parts;
        }

        const delay = Math.max(minDelay, Math.min(defaultDelay, maxDelay));

        for (let i = 0; i < messages.length; i++) {
            const postContent = markdownToPostContent(messages[i]);

            if (i === 0) {
                // 第一条作为回复
                await replyPost(this.context.messageId, postContent);
            } else {
                // 后续消息带延迟
                await sleep(delay);
                await sendPost(this.context.chatId, postContent);
            }
        }

        console.debug(
            `[TextReplyStrategy] 发送完成，共 ${messages.length} 条消息`,
        );
    }
}
