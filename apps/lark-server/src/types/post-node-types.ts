export type TextPostNodeType = 'bold' | 'underline' | 'lineThrough' | 'italic';

export interface TextPostNode {
    tag: 'text';
    text: string; // 文本内容
    un_escape?: boolean; // 是否 unescape 解码。默认为 false
    style?: TextPostNodeType[];
}

export interface LinkPostNode {
    tag: 'a';
    text: string; // 超链接的文本内容
    href: string; // 超链接地址
    style?: TextPostNodeType[];
}

export interface AtPostNode {
    tag: 'at';
    user_id: string; // 用户 ID，用来指定被 @ 的用户
    style?: TextPostNodeType[];
}

export interface ImgPostNode {
    tag: 'img';
    image_key: string; // 图片 Key
}

export interface MediaPostNode {
    tag: 'media';
    file_key: string; // 视频 Key
    image_key: string; // 图片 Key
}

export interface MdPostNode {
    tag: 'md';
    text: string; // 内容
}

export interface CodeBlockPostNode {
    tag: 'code_block';
    language?: string; // 代码块的语言类型
    text: string; // 代码块内容
}

export interface EmotionNode {
    tag: 'emotion';
    emoji_type: string; // 表情 Key
}

// 富文本消息中的每个节点
export type PostNode =
    | TextPostNode
    | ImgPostNode
    | LinkPostNode
    | AtPostNode
    | MediaPostNode
    | MdPostNode
    | CodeBlockPostNode
    | EmotionNode;
