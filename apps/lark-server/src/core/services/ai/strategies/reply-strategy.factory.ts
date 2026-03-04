import { ReplyStrategy, ReplyStrategyContext, ReplyMode } from './reply-strategy.interface';
import { CardReplyStrategy } from './card-reply.strategy';
import { MultiMessageReplyStrategy } from './multi-message-reply.strategy';
import { MultiMessageConfig, multiMessageConfig } from '@config/multi-message.config';
import { BaseChatInfoRepository } from '@infrastructure/dal/repositories/repositories';

/**
 * 回复策略工厂
 * 根据群聊灰度配置创建对应的回复策略
 */
export class ReplyStrategyFactory {
    constructor(private multiMsgConfig: MultiMessageConfig = multiMessageConfig) {}

    /**
     * 创建回复策略
     */
    async create(context: ReplyStrategyContext): Promise<ReplyStrategy> {
        const mode = await this.resolveMode(context);

        console.debug(`[ReplyStrategyFactory] chatId=${context.chatId}, 使用回复模式: ${mode}`);

        if (mode === 'multi_message') {
            return new MultiMessageReplyStrategy(context, this.multiMsgConfig);
        }

        // 默认使用卡片策略
        return new CardReplyStrategy(context);
    }

    /**
     * 从数据库读取灰度配置，判断回复模式
     */
    async resolveMode(context: ReplyStrategyContext): Promise<ReplyMode> {
        try {
            const chatInfo = await BaseChatInfoRepository.findOne({
                where: { chat_id: context.chatId },
            });

            // 优先检查 reply_mode 配置
            const replyMode = chatInfo?.gray_config?.reply_mode;
            if (replyMode === 'card') {
                return 'card';
            }
            if (replyMode === 'text') {
                return 'text';
            }

            // 兼容旧的 multi_message 配置
            const value = chatInfo?.gray_config?.multi_message;
            if (value === 'on' || value === 'true' || value === '1') {
                return 'multi_message';
            }
        } catch (error) {
            console.error('[ReplyStrategyFactory] 读取灰度配置失败:', error);
        }

        // 默认使用 text 模式（队列模式）
        return 'text';
    }
}

/**
 * 全局策略工厂实例
 */
let strategyFactory: ReplyStrategyFactory | null = null;

/**
 * 获取策略工厂单例
 */
export function getStrategyFactory(): ReplyStrategyFactory {
    if (!strategyFactory) {
        strategyFactory = new ReplyStrategyFactory();
    }
    return strategyFactory;
}
