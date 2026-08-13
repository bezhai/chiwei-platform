// 飞书送到我们手上的原始报文形状。这里只描述**线上真实收到的字段**，不做任何
// 解释 —— 解释是 parse-message 的事。
//
// 名字刻意贴着飞书自己的术语（message_type / mentions / chat_type），因为这一层
// 的唯一职责就是"飞书说了什么"。一旦有字段被重命名成我们的说法，读代码的人就再也
// 无法拿它跟飞书开放平台的文档对照。

/** 消息事件里的发送者。三种 id 都可能缺，取哪一种由使用方决定。 */
export interface LarkSender {
    sender_type: string;
    sender_id?: {
        union_id?: string;
        user_id?: string;
        open_id?: string;
    };
    tenant_key?: string;
}

/**
 * 一条被 @ 的对象。`key` 是正文里出现的那个 `@_user_N` 占位符本身 —— 飞书用它
 * 把正文里的位置和这条记录关联起来，所以按 key 查、而不是按数组下标查。
 */
export interface LarkMention {
    key: string;
    id: {
        union_id?: string;
        user_id?: string;
        open_id?: string;
    };
    name: string;
    tenant_key?: string;
    mentioned_type?: string; // "bot" | "user"
    bot_info?: {
        app_id?: string;
    };
}

export interface LarkMessageBody {
    message_id: string;
    root_id?: string;
    parent_id?: string;
    create_time: string;
    update_time?: string;
    chat_id: string;
    thread_id?: string;
    chat_type: string;
    message_type: string;
    /** 一段 JSON 字符串，形状由 message_type 决定。 */
    content: string;
    mentions?: LarkMention[];
    user_agent?: string;
}

/** `im.message.receive_v1` 的事件体。 */
export interface LarkMessageEvent {
    event_id?: string;
    create_time?: string;
    event_type?: string;
    tenant_key?: string;
    app_id?: string;
    sender: LarkSender;
    message: LarkMessageBody;
}

// ---- content 字段按 message_type 展开后的形状 ----

export interface LarkTextPayload {
    text: string;
}

export interface LarkImagePayload {
    image_key: string;
}

export interface LarkStickerPayload {
    file_key: string;
}

/** 视频。飞书把它叫 `media`，实际内容是一段视频加一张封面图。 */
export interface LarkVideoPayload {
    file_key: string;
    image_key?: string;
    file_name?: string;
    duration?: number;
}

export interface LarkFilePayload {
    file_key: string;
    file_name?: string;
}

export interface LarkAudioPayload {
    file_key: string;
    duration?: number;
}

/** 富文本节点。我们只渲染 text 和 img，其余标签原样跳过。 */
export interface LarkPostNode {
    tag: string;
    text?: string;
    image_key?: string;
    [key: string]: unknown;
}

export interface LarkPostPayload {
    title?: string;
    content: LarkPostNode[][];
}
