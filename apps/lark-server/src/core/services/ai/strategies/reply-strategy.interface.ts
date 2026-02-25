import { StreamAction } from 'types/ai';
import { ChatStateMachineCallbacks } from '../chat-state-machine';
import { ChatMessage } from 'types/chat';

/**
 * 回复策略上下文
 */
export interface ReplyStrategyContext {
    /** 用户消息ID */
    messageId: string;
    /** 聊天ID */
    chatId: string;
    /** 用户ID (union_id) */
    userId?: string;
    /** 是否为私聊 */
    isP2P: boolean;
    /** 根消息ID */
    rootId?: string;
}

/**
 * 保存消息的回调函数类型
 */
export type SaveMessageCallback = (content: string) => Promise<ChatMessage | undefined>;

/**
 * 回复策略接口
 * 定义了不同回复模式（卡片、多消息等）的统一接口
 */
export interface ReplyStrategy {
    /** 开始回复 */
    onStartReply(): Promise<void>;

    /** 流式内容处理 */
    onSend(action: StreamAction): Promise<void>;

    /** 回复成功 */
    onSuccess(content: string): Promise<void>;

    /** 回复失败 */
    onFailed(error: Error): Promise<void>;

    /** 结束清理 */
    onEnd(): Promise<void>;

    /** 获取回调对象（兼容现有 sseChat 接口） */
    getCallbacks(): ChatStateMachineCallbacks;
}

/**
 * 回复模式类型
 */
export type ReplyMode = 'card' | 'multi_message';
