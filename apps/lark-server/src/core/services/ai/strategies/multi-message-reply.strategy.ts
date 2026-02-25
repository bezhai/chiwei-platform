import { StreamAction } from 'types/ai';
import { ChatStateMachineCallbacks } from '../chat-state-machine';
import { ReplyStrategy, ReplyStrategyContext } from './reply-strategy.interface';
import { MultiMessageConfig } from '@config/multi-message.config';
import { replyPost, sendPost } from '@lark/basic/message';
import { markdownToPostContent } from 'core/services/message/post-content-processor';
import dayjs from 'dayjs';

/**
 * 休眠函数
 */
function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * 多消息回复策略
 * 将 AI 回复拆分为多条独立消息发送
 */
export class MultiMessageReplyStrategy implements ReplyStrategy {
    /** 已发送内容的长度（用于计算增量） */
    private sentLength = 0;
    private lastSendTime = 0;
    private messagesSent = 0;
    private isFirstMessage = true;
    private createTime: number;
    private firstMessageId?: string;
    /** 当前累积的完整内容 */
    private fullContent = '';

    constructor(
        private context: ReplyStrategyContext,
        private config: MultiMessageConfig,
    ) {
        this.createTime = dayjs().valueOf();
    }

    async onStartReply(): Promise<void> {
        // 多消息模式不创建卡片，只初始化状态
        this.sentLength = 0;
        this.lastSendTime = 0;
        this.messagesSent = 0;
        this.isFirstMessage = true;
        this.fullContent = '';
        console.debug('[MultiMessageStrategy] 开始多消息回复');
    }

    async onSend(action: StreamAction): Promise<void> {
        // 只处理 text 类型，忽略 think 和 status
        if (action.type !== 'text') {
            return;
        }

        // action.content 是累积的完整内容，直接使用
        this.fullContent = action.content;
        await this.flushCompleteMessages();
    }

    async onSuccess(content: string): Promise<void> {
        // 发送剩余未发送的内容
        const remaining = this.fullContent.substring(this.sentLength).replace(new RegExp(this.config.splitMarker, 'g'), '').trim();
        if (remaining) {
            await this.sendWithDelay(remaining);
        }
        console.debug(`[MultiMessageStrategy] 回复成功，共发送 ${this.messagesSent} 条消息`);
    }

    async onFailed(error: Error): Promise<void> {
        // 发送错误消息
        const errorMessage = `抱歉，出了点问题：${error.message}`;
        const postContent = markdownToPostContent(errorMessage);
        try {
            if (this.isFirstMessage) {
                await replyPost(this.context.messageId, postContent);
            } else {
                await sendPost(this.context.chatId, postContent);
            }
        } catch (sendError) {
            console.error('[MultiMessageStrategy] 发送错误消息失败:', sendError);
        }
    }

    async onEnd(): Promise<void> {
        // 清理状态
        this.fullContent = '';
        this.sentLength = 0;
        console.debug('[MultiMessageStrategy] 多消息回复结束');
    }

    getCallbacks(): ChatStateMachineCallbacks {
        return {
            onAccept: async () => {
                console.debug('[MultiMessageStrategy] 消息已被接收');
            },
            onStartReply: () => this.onStartReply(),
            onSend: (action) => this.onSend(action),
            onSuccess: (content) => this.onSuccess(content),
            onFailed: (error) => this.onFailed(error),
            onEnd: () => this.onEnd(),
        };
    }

    /**
     * 获取第一条消息的ID（用于保存消息）
     */
    getMessageId(): string | undefined {
        return this.firstMessageId;
    }

    /**
     * 获取创建时间（用于保存消息）
     */
    getCreateTime(): number {
        return this.createTime;
    }

    /**
     * 获取完整内容（用于保存消息）
     */
    getFullContent(): string {
        return this.fullContent;
    }

    // ========== 私有方法 ==========

    /**
     * 刷新并发送完整的消息
     * 基于累积内容和已发送长度计算待处理内容
     */
    private async flushCompleteMessages(): Promise<void> {
        const { splitMarker, maxMessages } = this.config;

        // 获取未处理的内容（从已发送位置开始）
        let pending = this.fullContent.substring(this.sentLength);

        while (pending.includes(splitMarker)) {
            const splitIndex = pending.indexOf(splitMarker);
            const message = pending.substring(0, splitIndex).trim();

            if (message) {
                // 检查是否需要合并（超过阈值）
                if (this.messagesSent >= maxMessages - 1) {
                    // 已发送 maxMessages-1 条，剩余内容合并到最后一条
                    console.debug(
                        `[MultiMessageStrategy] 已达到最大消息数 ${maxMessages}，剩余内容将合并`,
                    );
                    return;
                }

                await this.sendWithDelay(message);
            }

            // 更新已发送长度（包括分隔符）
            this.sentLength += splitIndex + splitMarker.length;
            pending = this.fullContent.substring(this.sentLength);
        }
    }

    /**
     * 带延迟发送消息
     */
    private async sendWithDelay(content: string): Promise<void> {
        if (!content) return;

        const { defaultDelay, minDelay, maxDelay } = this.config;

        // 间隔控制（第一条消息不等待）
        if (!this.isFirstMessage) {
            const elapsed = Date.now() - this.lastSendTime;
            const delay = Math.max(minDelay, Math.min(defaultDelay, maxDelay));
            if (elapsed < delay) {
                await sleep(delay - elapsed);
            }
        }

        try {
            // 发送消息（使用 post 格式以支持 markdown 和图片）
            const postContent = markdownToPostContent(content);
            if (this.isFirstMessage) {
                // 第一条消息作为回复
                console.debug(`[MultiMessageStrategy] 发送第一条消息（回复）: ${content.substring(0, 50)}...`);
                await replyPost(this.context.messageId, postContent);
                this.isFirstMessage = false;
                this.firstMessageId = this.context.messageId;
            } else {
                // 后续消息作为独立消息
                console.debug(`[MultiMessageStrategy] 发送第 ${this.messagesSent + 1} 条消息: ${content.substring(0, 50)}...`);
                await sendPost(this.context.chatId, postContent);
            }

            this.lastSendTime = Date.now();
            this.messagesSent++;
        } catch (error) {
            console.error('[MultiMessageStrategy] 发送消息失败:', error);
            throw error;
        }
    }
}
