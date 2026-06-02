export interface MessageBasicChatInfo {
    chat_id: string;
    chat_mode: 'group' | 'topic' | 'p2p';
    permission_config?: {
        allow_send_message?: boolean;
        allow_send_pixiv_image?: boolean;
        open_repeat_message?: boolean;
        allow_send_limit_photo?: boolean;
        can_access_restricted_models?: boolean;
        can_access_restricted_prompts?: boolean;
        new_permission?: boolean;
        is_canary?: boolean;
    };
    gray_config?: Record<string, string> | null;
    common_conversation_id?: string;
}

export interface MessageGroupChatInfo {
    chat_id: string;
    baseChatInfo?: MessageBasicChatInfo;
    name?: string;
    avatar?: string;
    user_count?: number;
    is_leave?: boolean;
    download_has_permission_setting?: 'all_members' | 'not_anyone';
}

export interface MessageSenderInfo {
    union_id?: string;
    name?: string;
    avatar_origin?: string;
    is_admin?: boolean;
}

export interface MessageMetadata {
    rootId?: string;
    threadId?: string;
    messageId: string;
    chatId: string;
    sender: string;
    senderOpenId?: string;
    senderName?: string | undefined;
    parentMessageId?: string;
    chatType: string;
    isRobotMessage: boolean;
    messageType?: string;
    createTime?: string;
    basicChatInfo?: MessageBasicChatInfo;
    groupChatInfo?: MessageGroupChatInfo;
    senderInfo?: MessageSenderInfo;
}

export class MessageMetadataUtils {
    static isP2P(metadata: MessageMetadata): boolean {
        return metadata.chatType === 'p2p';
    }
}
