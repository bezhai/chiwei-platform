/**
 * @file chat.ts
 * @description 大模型chat api 类型定义
 */

/**
 * 聊天消息
 */
export interface ChatMessage {
    /**
     * 用户id
     */
    user_id: string;

    /**
     * 转义成markdown的消息内容, 包括图片等
     */
    content: string;

    /**
     * 角色
     */
    role: 'user' | 'assistant';

    /**
     * 根消息id
     */
    root_message_id?: string;

    /**
     * 回复消息的id
     */
    reply_message_id?: string;

    /**
     * 消息id
     */
    message_id: string;

    /**
     * 聊天id
     */
    chat_id: string;

    /**
     * 聊天类型
     */
    chat_type: 'p2p' | 'group';

    /**
     * 创建时间
     */
    create_time: string;

    /**
     * 飞书原始消息类型
     */
    message_type?: string;

    /**
     * 机器人名称（用于多 bot 场景）
     */
    bot_name?: string;
}

/**
 * 聊天请求
 */
export interface ChatRequest {
    message_id: string; // 消息id / Message ID
    session_id?: string; // 会话追踪 ID
    is_canary?: boolean; // 是否开启灰度
}

/**
 * 接收到消息后，服务器返回的步骤
 */
export enum Step {
    /**
     * 收到消息
     */
    ACCEPT = 'accept',

    /**
     * 开始回复消息
     */
    START_REPLY = 'start_reply',

    /**
     * 发送消息
     */
    SEND = 'send',

    /**
     * 回复失败
     */
    FAILED = 'failed',

    /**
     * 回复成功
     */
    SUCCESS = 'success',

    /**
     * 结束
     */
    END = 'end',
}

/**
 * 聊天返回
 */


interface ChatProcessResponse {
    /**
     * 步骤
     */
    step: Step.SEND | Step.SUCCESS;

    /**
     * 思维链内容
     */
    reason_content?: string;

    /**
     * 回复内容
     */
    content?: string;
}

interface ChatNormalResponse {
    /**
     * 步骤
     */
    step: Exclude<Step, Step.SEND | Step.SUCCESS>;
}

interface ChatStatusResponse {
    /**
     * 步骤
     */
    step: Step.SEND;

    /**
     * 状态消息
     */
    status_message: string;
}

export type ChatResponse = ChatProcessResponse | ChatNormalResponse | ChatStatusResponse;

export interface StoreRobotMessageRequest {
    message: ChatMessage;
}

export interface StoreRobotMessageResponse {
    code: number;
    message: string;
}
