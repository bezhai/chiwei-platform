import { LarkCard } from 'feishu-card';
import { PostContent, TextContent, ImageContent, StickerContent } from './content-types';

export interface SendPostContent {
    zh_cn: PostContent; // 中文
}

export type SendContent = TextContent | ImageContent | StickerContent | SendPostContent | LarkCard;
