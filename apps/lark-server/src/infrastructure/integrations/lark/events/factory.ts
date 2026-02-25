import { Message } from 'core/models/message';
import {
    TextContent,
    ImageContent,
    StickerContent,
    PostContent,
    MediaContent,
    FileContent,
    AudioContent,
} from 'types/content-types';
import { LarkReceiveMessage } from 'types/lark';
import { ContentType, ContentItem } from 'core/models/message-content';
import { MentionUtils } from '@lark/utils/mention-utils';

export interface MessageContentHandler {
    generateContent(): ContentItem[];
}

abstract class BaseMessageContentFactory implements MessageContentHandler {
    protected content: string;

    constructor(content: string) {
        this.content = content;
    }

    abstract generateContent(): ContentItem[];
}

class TextMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        try {
            const content: TextContent = JSON.parse(this.content);
            return [
                {
                    type: ContentType.Text,
                    value: content.text,
                },
            ];
        } catch (err) {
            console.error('Failed to parse text content:', err);
            return [{ type: ContentType.Text, value: '[文本]' }];
        }
    }
}

class ImageMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        try {
            const content: ImageContent = JSON.parse(this.content);
            return [
                {
                    type: ContentType.Image,
                    value: content.image_key,
                },
            ];
        } catch (err) {
            console.error('Failed to parse image content:', err);
            return [{ type: ContentType.Text, value: '[图片]' }];
        }
    }
}

class StickerMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        try {
            const content: StickerContent = JSON.parse(this.content);
            return [
                {
                    type: ContentType.Sticker,
                    value: content.file_key,
                },
            ];
        } catch (err) {
            console.error('Failed to parse sticker content:', err);
            return [{ type: ContentType.Text, value: '[表情包]' }];
        }
    }
}

class PostMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        try {
            const content: PostContent = JSON.parse(this.content);
            const items: ContentItem[] = [];

            content.content.forEach((row) => {
                row.forEach((node) => {
                    if (node.tag === 'text' && node.text) {
                        items.push({
                            type: ContentType.Text,
                            value: node.text,
                        });
                    } else if (node.tag === 'img' && node.image_key) {
                        items.push({
                            type: ContentType.Image,
                            value: node.image_key,
                        });
                    }
                });
            });

            return items.length > 0 ? items : [{ type: ContentType.Text, value: '[富文本]' }];
        } catch (err) {
            console.error('Failed to parse post content:', err);
            return [{ type: ContentType.Text, value: '[富文本]' }];
        }
    }
}

class MediaMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        try {
            const content: MediaContent = JSON.parse(this.content);
            return [
                {
                    type: ContentType.Media,
                    value: content.file_key,
                    meta: {
                        image_key: content.image_key,
                        file_name: content.file_name,
                        duration: content.duration,
                    },
                },
            ];
        } catch (err) {
            console.error('Failed to parse media content:', err);
            return [{ type: ContentType.Text, value: '[视频]' }];
        }
    }
}

class FileMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        try {
            const content: FileContent = JSON.parse(this.content);
            return [
                {
                    type: ContentType.File,
                    value: content.file_key,
                    meta: {
                        file_name: content.file_name,
                    },
                },
            ];
        } catch (err) {
            console.error('Failed to parse file content:', err);
            return [{ type: ContentType.Text, value: '[文件]' }];
        }
    }
}

class AudioMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        try {
            const content: AudioContent = JSON.parse(this.content);
            return [
                {
                    type: ContentType.Audio,
                    value: content.file_key,
                    meta: {
                        duration: content.duration,
                    },
                },
            ];
        } catch (err) {
            console.error('Failed to parse audio content:', err);
            return [{ type: ContentType.Text, value: '[语音]' }];
        }
    }
}

class MergeForwardMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        return [
            {
                type: ContentType.Unsupported,
                value: '[合并转发]',
                meta: { original_type: 'merge_forward' },
            },
        ];
    }
}

class ShareChatMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        return [
            {
                type: ContentType.Unsupported,
                value: '[分享群名片]',
                meta: { original_type: 'share_chat' },
            },
        ];
    }
}

class ShareUserMessageContentFactory extends BaseMessageContentFactory {
    generateContent(): ContentItem[] {
        return [
            {
                type: ContentType.Unsupported,
                value: '[分享个人名片]',
                meta: { original_type: 'share_user' },
            },
        ];
    }
}

class UnsupportedMessageContentFactory extends BaseMessageContentFactory {
    private messageType: string;

    constructor(content: string, messageType: string) {
        super(content);
        this.messageType = messageType;
    }

    generateContent(): ContentItem[] {
        return [
            {
                type: ContentType.Unsupported,
                value: `[${this.messageType}]`,
                meta: { original_type: this.messageType },
            },
        ];
    }
}

type FactoryConstructor = new (content: string) => BaseMessageContentFactory;

const factoryRegistry: Record<string, FactoryConstructor> = {
    text: TextMessageContentFactory,
    image: ImageMessageContentFactory,
    post: PostMessageContentFactory,
    sticker: StickerMessageContentFactory,
    media: MediaMessageContentFactory,
    file: FileMessageContentFactory,
    audio: AudioMessageContentFactory,
    merge_forward: MergeForwardMessageContentFactory,
    share_chat: ShareChatMessageContentFactory,
    share_user: ShareUserMessageContentFactory,
};

export class MessageTransferer {
    static getContentFactory(messageType: string, content: string): MessageContentHandler {
        const FactoryClass = factoryRegistry[messageType];
        if (FactoryClass) {
            return new FactoryClass(content);
        }
        return new UnsupportedMessageContentFactory(content, messageType);
    }

    static async transfer(event: LarkReceiveMessage): Promise<Message | null> {
        const contentFactory = this.getContentFactory(
            event.message.message_type,
            event.message.content,
        );
        const items = contentFactory.generateContent();
        if (items.length === 0) {
            console.warn('Failed to generate content:', event.message.message_id);
            return null;
        }
        return Message.fromEvent(event, {
            items,
            mentions: MentionUtils.addMentions(event.message.mentions),
            mentionMap: MentionUtils.addMentionMap(event.message.mentions),
        });
    }
}
