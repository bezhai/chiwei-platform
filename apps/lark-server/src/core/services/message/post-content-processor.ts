import { PostContent } from 'types/content-types';
import { PostNode, TextPostNode, AtPostNode, EmotionNode, MdPostNode, ImgPostNode } from 'types/post-node-types';
import { emojiService } from 'infrastructure/crontab/services/emoji';

/**
 * 从文本中提取形如 [xxx] 的子串
 */
function extractEmojiTexts(text: string): string[] {
    const regex = /\[([^\]]+)\]/g;
    const matches: string[] = [];
    let match;

    while ((match = regex.exec(text)) !== null) {
        matches.push(match[1]);
    }

    return matches;
}

/**
 * 处理单个文本片段，识别 @提及
 */
function processTextSegment(text: string): PostNode[] {
    const nodes: PostNode[] = [];

    // 正则表达式匹配 <at user_id="xxx"></at> 格式
    const atRegex = /<at user_id="([^"]+)"><\/at>/g;
    let lastIndex = 0;
    let match;

    while ((match = atRegex.exec(text)) !== null) {
        // 添加 @ 符号前的文本
        if (match.index > lastIndex) {
            const textBefore = text.substring(lastIndex, match.index);
            if (textBefore.trim()) {
                nodes.push({
                    tag: 'text',
                    text: textBefore,
                } as TextPostNode);
            }
        }

        // 添加 @提及节点
        nodes.push({
            tag: 'at',
            user_id: match[1],
        } as AtPostNode);

        lastIndex = match.index + match[0].length;
    }

    // 添加最后的文本
    if (lastIndex < text.length) {
        const remainingText = text.substring(lastIndex);
        if (remainingText.trim()) {
            nodes.push({
                tag: 'text',
                text: remainingText,
            } as TextPostNode);
        }
    }

    return nodes;
}

/**
 * 将文本转换为 PostContent，支持 emoji 表情和 @提及
 */
export async function createPostContentFromText(text: string): Promise<PostContent> {
    // 提取所有形如 [xxx] 的子串
    const emojiTexts = extractEmojiTexts(text);

    // 批量查询 emoji 数据
    const emojis = await emojiService.getEmojiByText(emojiTexts);

    // 创建 emoji 文本到 key 的映射
    const emojiMap = new Map<string, string>();
    emojis.forEach(emoji => {
        emojiMap.set(emoji.text, emoji.key);
    });

    // 分割文本处理 emoji
    const contents: PostNode[] = [];
    let lastIndex = 0;
    const emojiRegex = /\[([^\]]+)\]/g;
    let match;

    while ((match = emojiRegex.exec(text)) !== null) {
        const emojiText = match[1];
        const emojiKey = emojiMap.get(emojiText);

        // 处理 emoji 前的文本（包含可能的 @提及）
        if (match.index > lastIndex) {
            const textBefore = text.substring(lastIndex, match.index);
            const processedNodes = processTextSegment(textBefore);
            contents.push(...processedNodes);
        }

        // 添加 emoji（如果找到对应的 key）
        if (emojiKey) {
            contents.push({
                tag: 'emotion',
                emoji_type: emojiKey,
            } as EmotionNode);
        } else {
            // 如果没找到对应的 emoji，将 [xxx] 当作普通文本处理
            contents.push({
                tag: 'text',
                text: `[${emojiText}]`,
            } as TextPostNode);
        }

        lastIndex = match.index + match[0].length;
    }

    // 处理最后的文本（包含可能的 @提及）
    if (lastIndex < text.length) {
        const remainingText = text.substring(lastIndex);
        const processedNodes = processTextSegment(remainingText);
        contents.push(...processedNodes);
    }

    // 如果没有任何内容，返回空文本
    if (contents.length === 0) {
        return {
            content: [[{
                tag: 'text',
                text: text,
            } as TextPostNode]],
        };
    }

    return { content: [contents] };
}

/**
 * 将 markdown 文本转换为 PostContent，识别 ![alt](image_key) 图片语法
 * 图片之间的文本使用 md 节点渲染（支持加粗、斜体等 markdown 格式）
 */
export function markdownToPostContent(markdown: string): PostContent {
    const IMAGE_PATTERN = /!\[.*?\]\(([^)]+)\)/g;
    const content: PostNode[][] = [];
    let lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = IMAGE_PATTERN.exec(markdown)) !== null) {
        if (match.index > lastIndex) {
            const text = markdown.slice(lastIndex, match.index).trim();
            if (text) {
                content.push([{ tag: 'md', text } as MdPostNode]);
            }
        }
        content.push([{ tag: 'img', image_key: match[1] } as ImgPostNode]);
        lastIndex = match.index + match[0].length;
    }

    if (lastIndex < markdown.length) {
        const text = markdown.slice(lastIndex).trim();
        if (text) {
            content.push([{ tag: 'md', text } as MdPostNode]);
        }
    }

    if (content.length === 0) {
        content.push([{ tag: 'md', text: markdown } as MdPostNode]);
    }

    return { content };
}
