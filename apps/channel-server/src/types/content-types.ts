import { PostNode } from './post-node-types';

// 文本消息内容
export interface TextContent {
    text: string; // 文本内容，必填
}

// 图片消息内容
export interface ImageContent {
    image_key: string; // 图片的 key，必填
}

// 表情包消息内容
export interface StickerContent {
    file_key: string; // 表情包文件的 key，必填
}

// 富文本消息内容
export interface PostContent {
    title?: string; // 富文本消息的标题
    content: PostNode[][]; // 富文本消息的内容，二维数组
}

// 视频消息内容
export interface MediaContent {
    file_key: string;
    image_key?: string;
    file_name?: string;
    duration?: number;
}

// 文件消息内容
export interface FileContent {
    file_key: string;
    file_name?: string;
}

// 语音消息内容
export interface AudioContent {
    file_key: string;
    duration?: number;
}

// 合并转发消息内容
export interface MergeForwardContent {
    [key: string]: unknown;
}

// 分享群名片消息内容
export interface ShareChatContent {
    chat_id: string;
}

// 分享个人名片消息内容
export interface ShareUserContent {
    user_id: string;
}

export type Content =
    | TextContent
    | ImageContent
    | StickerContent
    | PostContent
    | MediaContent
    | FileContent
    | AudioContent
    | MergeForwardContent
    | ShareChatContent
    | ShareUserContent;
