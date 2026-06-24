import {
    MessageMetadata,
    MessageMetadataUtils,
    type MessageBasicChatInfo,
    type MessageSenderInfo,
} from './message-metadata';
import { type ContentItem, MessageContent, MessageContentUtils } from './message-content';

export class Message {
    private metadata: MessageMetadata;
    private content: MessageContent;

    constructor(metadata: MessageMetadata, content: MessageContent) {
        this.metadata = metadata;
        this.content = content;
    }

    // Metadata accessors
    get messageId(): string {
        return this.metadata.messageId;
    }

    get rootId(): string | undefined {
        return this.metadata.rootId;
    }

    get parentMessageId(): string | undefined {
        return this.metadata.parentMessageId;
    }

    get threadId(): string | undefined {
        return this.metadata.threadId;
    }

    get chatId(): string {
        return this.metadata.chatId;
    }

    get sender(): string {
        return this.metadata.sender;
    }

    get senderOpenId(): string | undefined {
        return this.metadata.senderOpenId;
    }

    get senderName(): string | undefined {
        return this.metadata.senderName;
    }

    get chatType(): string {
        return this.metadata.chatType;
    }

    get isRobotMessage(): boolean {
        return this.metadata.isRobotMessage;
    }

    get messageType(): string | undefined {
        return this.metadata.messageType;
    }

    get basicChatInfo(): MessageBasicChatInfo | undefined {
        return this.metadata.basicChatInfo;
    }

    get groupChatInfo() {
        return this.metadata.groupChatInfo;
    }

    get senderInfo(): MessageSenderInfo | undefined {
        return this.metadata.senderInfo;
    }

    get createTime(): string | undefined {
        return this.metadata.createTime;
    }

    isP2P(): boolean {
        return MessageMetadataUtils.isP2P(this.metadata);
    }

    // Content accessors
    texts(): string[] {
        return MessageContentUtils.texts(this.content);
    }

    text(): string {
        return MessageContentUtils.fullText(this.content);
    }

    clearText(): string {
        return MessageContentUtils.clearText(this.content);
    }

    withoutEmojiText(): string {
        return MessageContentUtils.withoutEmojiText(this.content);
    }

    imageKeys(): string[] {
        return MessageContentUtils.imageKeys(this.content);
    }

    fileKeys(): string[] {
        return MessageContentUtils.fileKeys(this.content);
    }

    stickerKey(): string {
        return MessageContentUtils.stickerKey(this.content);
    }

    isTextOnly(): boolean {
        return MessageContentUtils.isTextOnly(this.content);
    }

    isStickerOnly(): boolean {
        return MessageContentUtils.isStickerOnly(this.content);
    }

    hasMention(mentionId: string): boolean {
        return this.content.mentions.some((mention) => mention.id === mentionId);
    }

    getMentionedUsers(): string[] {
        return this.content.mentions.map((mention) => mention.id);
    }

    /**
     * 从 mention 列表中找到第一个真实用户（排除所有 bot mention）
     */
    getFirstMentionedHuman(): string | undefined {
        return this.content.mentions.find((mention) => !mention.botCommonUserId)?.id;
    }

    contentItems(): readonly ContentItem[] {
        return this.content.items;
    }

    // For debugging
    toJSON() {
        return {
            metadata: this.metadata,
            content: this.content,
        };
    }

    toMarkdown(): string {
        return MessageContentUtils.toMarkdown(this.content, this.allowDownloadResource());
    }

    toStorageFormat(excludeBotCommonUserId?: string): string {
        const mentions = this.content.mentions
            .map((mention) => {
                if (mention.botCommonUserId === excludeBotCommonUserId) return null;
                return { user_id: mention.id, name: mention.displayName };
            })
            .filter((m): m is NonNullable<typeof m> => m !== null);

        return JSON.stringify({
            v: 2,
            text: this.toMarkdown(),
            items: this.content.items.map((item) => ({
                type: item.type,
                value: item.value,
                ...(item.meta ? { meta: item.meta } : {}),
            })),
            ...(mentions.length > 0 ? { mentions } : {}),
        });
    }

    allowDownloadResource(): boolean {
        return this.metadata.groupChatInfo
            ? this.metadata.groupChatInfo.download_has_permission_setting === 'all_members'
            : true;
    }
}
