/**
 * QQ 网关 ↔ channel-server 的 custom wire 协议（平台无关归一化消息）。
 *
 * 网关把 QQ 原始事件归一化成 CustomInboundMessage 推给 channel-server；
 * channel-server 把回复表达成 CustomOutboundMessage 发回网关。
 * channel-server 因此保持零 QQ SDK 依赖，底层换实现对它透明。
 *
 * 注意：本协议只覆盖 QQ 当前支持的私聊 / 群聊两种会话（不含频道 channel）。
 */

/** 会话类型：私聊 / 群聊。 */
export type CustomChatType = 'direct' | 'group';

/** 入站附件（图片 / 语音 / 文件等，url 由网关原样透传）。 */
export interface CustomInboundAttachment {
    contentType: string;
    url: string;
    filename?: string;
    size?: number;
    width?: number;
    height?: number;
    /** 语音附件转出的 wav url（如有）。 */
    voiceWavUrl?: string;
    /** 语音 ASR 文本（如有）。 */
    asrText?: string;
}

/** 入站提及（用于群聊是否唤起本 bot 的判定）。 */
export interface CustomInboundMention {
    id?: string;
    userId?: string;
    memberId?: string;
    name?: string;
    isBot?: boolean;
    /** 是否 @ 了本 bot——群聊唤起判定的关键字段。 */
    isSelf?: boolean;
}

/** 入站引用消息信息。 */
export interface CustomInboundQuote {
    refId?: string;
    messageId?: string;
    content?: string;
    senderId?: string;
    senderName?: string;
    attachments?: CustomInboundAttachment[];
}

/**
 * 网关 → channel-server：已归一化的入站消息。
 */
export interface CustomInboundMessage {
    /** 哪个 bot 收到的，channel-server 据此查 bot_config。 */
    botName: string;
    chatType: CustomChatType;
    /** 私聊填用户会话 id，群聊填群 id。 */
    conversationId: string;
    /** 私聊 user_openid / 群 member_openid。 */
    senderId: string;
    senderName?: string;
    senderIsBot?: boolean;
    text: string;
    /** QQ 原始 msg_id，被动回复要回带。 */
    messageId: string;
    /** ISO 字符串。 */
    timestamp: string;
    attachments?: CustomInboundAttachment[];
    mentions?: CustomInboundMention[];
    quote?: CustomInboundQuote;
    /** 原始协议包，仅调试 / 少量扩展用，业务不应强依赖。 */
    raw?: unknown;
}

/**
 * channel-server → 网关：要发出的回复。
 *
 * 被动窗口由网关独占：出站必须带 replyToMessageId（所回应的原始 QQ msg_id），
 * 网关据此对同一 msg_id 维护 msg_seq 递增、60min 窗口与 4 次上限。
 * 缺 replyToMessageId 即视为主动发，网关会丢弃（QQ 官方机器人发不出主动消息）。
 */
export interface CustomOutboundMessage {
    /** 用哪个 bot 发。 */
    botName: string;
    chatType: CustomChatType;
    conversationId: string;
    /** 被动回复所回应的原始 QQ msg_id；缺失即视为主动发，网关会丢弃。 */
    replyToMessageId?: string;
    text?: string;
    /** 出站富媒体后置，本期可空。 */
    mediaUrls?: string[];
    /** 多段回复序号，0 起；网关据此在同一 replyToMessageId 下递增 msg_seq。 */
    partIndex?: number;
    isLast?: boolean;
    /** 幂等键，防 MQ 重投重发；由调用方基于全局 message id + 段序生成。 */
    idempotencyKey: string;
    /** 原始扩展数据，仅调试用。 */
    raw?: unknown;
}

/**
 * 网关 → channel-server：一条出站消息的发送回执。
 *
 * 网关是被动窗口 / 4 次上限 / 主动发拦截的唯一裁决方，channel-server 必须从这个
 * 结构得知「到底发出去没有」：
 *   - 真发出：sent=true，messageId 为 QQ 返回的新 msg_id（落库、续段锚点都用它）。
 *   - 丢弃 / 失败：sent=false，reason 说明原因（超窗 / 超 4 次 / 主动发 / 发送报错）。
 * channel-server 见 sent=false 必须抛错、不得用合成 id 兜底落库（否则把没发出的
 * 消息污染进 qq_message）。
 */
export interface CustomOutboundResult {
    sent: boolean;
    /** 发送成功时 QQ 返回的新 msg_id。 */
    messageId?: string;
    /** 丢弃 / 失败原因（sent=false 时给出）。 */
    reason?: string;
}
