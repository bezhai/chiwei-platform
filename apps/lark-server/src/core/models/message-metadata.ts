import { LarkBaseChatInfo, LarkGroupChatInfo, LarkUser } from 'infrastructure/dal/entities';

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
    basicChatInfo?: LarkBaseChatInfo;
    groupChatInfo?: LarkGroupChatInfo;
    senderInfo?: LarkUser;
}

export class MessageMetadataUtils {
    static isP2P(metadata: MessageMetadata): boolean {
        return metadata.chatType === 'p2p';
    }
}
