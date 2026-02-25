/**
 * Lark 客户端配置
 */
export interface LarkClientConfig {
    /** 应用 ID */
    appId: string;
    /** 应用密钥 */
    appSecret: string;
    /** 机器人名称（可选，用于多机器人场景） */
    botName?: string;
}

/**
 * 从环境变量创建默认配置
 */
export function createDefaultLarkConfig(): LarkClientConfig {
    return {
        appId: process.env.APP_ID || process.env.FEISHU_APP_ID || '',
        appSecret: process.env.APP_SECRET || process.env.FEISHU_APP_SECRET || '',
    };
}

/**
 * Lark API 响应
 */
export interface LarkResponse<T> {
    code?: number;
    msg?: string;
    data?: T;
}

/**
 * 消息类型
 */
export type MessageType = 'text' | 'image' | 'interactive' | 'sticker' | 'post' | 'file';

/**
 * 接收者 ID 类型
 */
export type ReceiveIdType = 'chat_id' | 'open_id' | 'user_id' | 'union_id' | 'email';

/**
 * 发送消息参数
 */
export interface SendMessageParams {
    /** 接收者 ID */
    receiveId: string;
    /** 接收者 ID 类型 */
    receiveIdType?: ReceiveIdType;
    /** 消息内容 */
    content: any;
    /** 消息类型 */
    msgType: MessageType;
}

/**
 * 回复消息参数
 */
export interface ReplyMessageParams {
    /** 消息 ID */
    messageId: string;
    /** 消息内容 */
    content: any;
    /** 消息类型 */
    msgType: MessageType;
    /** 是否在话题中回复 */
    replyInThread?: boolean;
}

/**
 * 获取消息列表参数
 */
export interface GetMessageListParams {
    /** 会话 ID */
    chatId: string;
    /** 开始时间（Unix 时间戳，秒） */
    startTime?: number;
    /** 结束时间（Unix 时间戳，秒） */
    endTime?: number;
    /** 分页 Token */
    pageToken?: string;
    /** 每页数量 */
    pageSize?: number;
}

/**
 * 群聊成员信息
 */
export interface ChatMember {
    member_id?: string;
    member_id_type?: string;
    name?: string;
    tenant_key?: string;
}

/**
 * 群聊信息
 */
export interface ChatInfo {
    chat_id?: string;
    name?: string;
    description?: string;
    owner_id?: string;
    owner_id_type?: string;
    chat_mode?: string;
    chat_type?: string;
    external?: boolean;
    tenant_key?: string;
}

/**
 * 用户信息
 */
export interface UserInfo {
    union_id?: string;
    user_id?: string;
    open_id?: string;
    name?: string;
    en_name?: string;
    nickname?: string;
    email?: string;
    mobile?: string;
    avatar?: {
        avatar_72?: string;
        avatar_240?: string;
        avatar_640?: string;
        avatar_origin?: string;
    };
}

/**
 * 消息信息
 */
export interface MessageInfo {
    message_id?: string;
    root_id?: string;
    parent_id?: string;
    msg_type?: string;
    create_time?: string;
    update_time?: string;
    deleted?: boolean;
    chat_id?: string;
    sender?: {
        id?: string;
        id_type?: string;
        sender_type?: string;
        tenant_key?: string;
    };
    body?: {
        content?: string;
    };
}

/**
 * 错误码映射
 */
export const ERROR_CODE_MAP: Record<number, string> = {
    41050: '无用户权限，请将当前操作的用户添加到应用或用户的权限范围内',
    99991663: '消息已被撤回或删除',
    99991668: '机器人不在群聊中',
    99991672: '机器人没有发送消息的权限',
};
