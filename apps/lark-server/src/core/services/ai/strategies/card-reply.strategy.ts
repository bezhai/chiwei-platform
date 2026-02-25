import { StreamAction } from 'types/ai';
import { ChatStateMachineCallbacks } from '../chat-state-machine';
import { ReplyStrategy, ReplyStrategyContext } from './reply-strategy.interface';
import { CardLifecycleManager } from '@lark/basic/card-lifecycle-manager';
import { multiMessageConfig } from '@config/multi-message.config';

/**
 * 卡片回复策略
 * 封装现有的 CardLifecycleManager 逻辑
 */
export class CardReplyStrategy implements ReplyStrategy {
    private cardManager: CardLifecycleManager;

    constructor(private context: ReplyStrategyContext) {
        this.cardManager = CardLifecycleManager.init();
        this.cardManager.appendCardContext({
            parent_message_id: context.messageId,
            chat_id: context.chatId,
            root_id: context.rootId,
            is_p2p: context.isP2P,
            union_id: context.userId,
        });
    }

    async onStartReply(): Promise<void> {
        // 创建卡片并回复消息
        await this.cardManager.createStartReplyHandler()();
        await this.cardManager.createReplyToMessageHandler(this.context.messageId)();
    }

    async onSend(action: StreamAction): Promise<void> {
        // 过滤掉多消息分隔符，避免在卡片中显示
        if (action.type === 'text') {
            action = {
                ...action,
                content: action.content.replace(new RegExp(multiMessageConfig.splitMarker, 'g'), ''),
            };
        }
        const handler = this.cardManager.createActionHandler();
        await handler(action);
    }

    async onSuccess(content: string): Promise<void> {
        // 过滤掉多消息分隔符
        const filteredContent = content.replace(new RegExp(multiMessageConfig.splitMarker, 'g'), '');
        await this.cardManager.createSuccessHandler()(filteredContent);
    }

    async onFailed(error: Error): Promise<void> {
        await this.cardManager.createFailedHandler()(error);
    }

    async onEnd(): Promise<void> {
        await this.cardManager.createEndHandler()();
    }

    getCallbacks(): ChatStateMachineCallbacks {
        return {
            onAccept: this.cardManager.createAcceptHandler(),
            onStartReply: () => this.onStartReply(),
            onSend: (action) => this.onSend(action),
            onSuccess: (content) => this.onSuccess(content),
            onFailed: (error) => this.onFailed(error),
            onEnd: () => this.onEnd(),
        };
    }

    /**
     * 获取消息ID（用于保存消息）
     */
    getMessageId(): string | undefined {
        return this.cardManager.getMessageId();
    }

    /**
     * 获取创建时间（用于保存消息）
     */
    getCreateTime(): number {
        return this.cardManager.getCreateTime();
    }
}
