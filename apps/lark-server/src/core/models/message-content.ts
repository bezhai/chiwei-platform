import { TextUtils } from '@inner/shared';
import { getBotUnionId } from '@core/services/bot/bot-var';

export enum ContentType {
    Text = 'text',
    Image = 'image',
    Sticker = 'sticker',
    Media = 'media',
    File = 'file',
    Audio = 'audio',
    Unsupported = 'unsupported',
}

export interface ContentItem {
    type: ContentType;
    value: string;
    meta?: Record<string, unknown>;
}

export interface MessageContent {
    items: ContentItem[];
    mentions: string[];
    mentionMap?: Record<
        string,
        {
            name: string;
            openId: string;
        }
    >;
}

export class MessageContentUtils {
    static toMarkdown(content: MessageContent, allowDownload: boolean): string {
        let markdown = content.items
            .map((item) => {
                if (item.type === ContentType.Text) {
                    return item.value;
                }
                if (item.type === ContentType.Image) {
                    if (!allowDownload) {
                        return '[Non-downloadable Image]';
                    }
                    return `![image](${item.value})`;
                }
                if (item.type === ContentType.Sticker) {
                    return `[表情包]`;
                }
                if (item.type === ContentType.Media) {
                    const fileName = item.meta?.file_name as string | undefined;
                    return fileName ? `[视频: ${fileName}]` : '[视频]';
                }
                if (item.type === ContentType.File) {
                    const fileName = item.meta?.file_name as string | undefined;
                    return fileName ? `[文件: ${fileName}]` : '[文件]';
                }
                if (item.type === ContentType.Audio) {
                    return '[语音]';
                }
                if (item.type === ContentType.Unsupported) {
                    return item.value;
                }
            })
            .join('');

        return this.resolveMentions(markdown, content);
    }

    static resolveMentions(text: string, content: MessageContent): string {
        let result = text;
        content.mentions.forEach((mention, index) => {
            const mentionInfo = content.mentionMap?.[mention];
            if (mentionInfo) {
                const botUnionId = getBotUnionId();
                const displayName = mention === botUnionId ? '赤尾' : mentionInfo.name;
                result = result.replace(`@_user_${index + 1}`, `@${displayName}`);
            }
        });
        return result;
    }

    static texts(content: MessageContent): string[] {
        return content.items
            .filter((item) => item.type === ContentType.Text)
            .map((item) => item.value);
    }

    static imageKeys(content: MessageContent): string[] {
        return content.items
            .filter((item) => item.type === ContentType.Image)
            .map((item) => item.value);
    }

    static stickerKey(content: MessageContent): string {
        const stickerItem = content.items.find((item) => item.type === ContentType.Sticker);
        return stickerItem?.value ?? '';
    }

    static fullText(content: MessageContent): string {
        return this.texts(content).join('');
    }

    static clearText(content: MessageContent): string {
        return TextUtils.clearText(this.fullText(content));
    }

    static withoutEmojiText(content: MessageContent): string {
        return TextUtils.removeEmoji(this.clearText(content));
    }

    static withMentionText(content: MessageContent): string {
        let text = this.fullText(content);
        content.mentions.forEach((mention, index) => {
            text = text.replace(`@_user_${index + 1}`, `<at user_id="${mention}"></at>`);
        });
        return text;
    }

    static isTextOnly(content: MessageContent): boolean {
        return content.items.every((item) => item.type === ContentType.Text);
    }

    static isStickerOnly(content: MessageContent): boolean {
        return content.items.length === 1 && content.items[0].type === ContentType.Sticker;
    }

    static wrapTextAsV2(text: string): string {
        return JSON.stringify({
            v: 2,
            text,
            items: [{ type: ContentType.Text, value: text }],
        });
    }

    /**
     * 将 LLM 输出的 markdown 解析为 v2 结构化 content。
     * 识别 ![image](image_key) 模式，拆分为 text + image 混合 items。
     * 这是 toMarkdown 的逆操作。
     */
    static wrapMarkdownAsV2(markdown: string): string {
        const IMAGE_PATTERN = /!\[.*?\]\(([^)]+)\)/g;
        const items: ContentItem[] = [];

        let lastIndex = 0;
        let match: RegExpExecArray | null;

        while ((match = IMAGE_PATTERN.exec(markdown)) !== null) {
            // match 之前的文本部分
            if (match.index > lastIndex) {
                items.push({
                    type: ContentType.Text,
                    value: markdown.slice(lastIndex, match.index),
                });
            }
            // image item
            items.push({
                type: ContentType.Image,
                value: match[1],
            });
            lastIndex = match.index + match[0].length;
        }

        // 剩余文本
        if (lastIndex < markdown.length) {
            items.push({
                type: ContentType.Text,
                value: markdown.slice(lastIndex),
            });
        }

        // 无图片，退化为纯文本
        if (items.length === 0) {
            items.push({ type: ContentType.Text, value: markdown });
        }

        return JSON.stringify({ v: 2, text: markdown, items });
    }
}
