// channel 接入的四层契约。这不是"飞书+QQ 的最小公倍数"，是通用接入契约：
// 任何能把消息送进来、把回复送回去的接入点都是一个 channel。契约里刻意不出现
// @ / 私聊群聊二元 / 回复树这些 IM 专有概念作为强制语义——它们只能活在各
// channel 自己的 adapter 内部。判断字段能否进契约的唯一标准：换一个非 IM
// channel（如纯 HTTP 问答入口）它是否还讲得通。

// ---- 内容 ----

// 这期只有纯文本；kind 留作扩展位（图片 / 富文本等后续迭代再加）。
export type ContentItem = { kind: 'text'; text: string };

// ---- 线程 / 关联引用 ----

// IM 的"回复某条消息 / 话题根"放这里；没有回复语义的 channel 直接传 null。
export interface ThreadRef {
    replyToChannelMessageId?: string;
    rootChannelMessageId?: string;
}

// ---- 寻址线索 ----

// channel 给的"这条消息冲谁来"的线索。IM 里就是被 @ 的对象；HTTP 入口可能为空。
// 由 AddressingPolicy 解释，契约不假设它一定是 @。
export interface AddressingHint {
    targetId: string;
}

// ---- 通用入站消息 ----

// 所有 InboundAdapter.parse 都产出这同一个结构。身份字段带 channel_ 前缀，
// 表示是"channel 内的 ID"，还没经 IdentityResolver 翻译成全局 ID。
export interface InboundMessage {
    channel: string;
    bot_name: string;
    channel_message_id: string;
    channel_chat_id: string;
    channel_user_id: string;
    // 会话作用域。常见 "direct"/"group"，但不是强制二元；非 IM channel 可定义
    // 自己的取值，adapter 负责把它映射到下游需要的 is_direct。
    conversation_scope: string;
    thread_ref: ThreadRef | null;
    addressing_hints: AddressingHint[];
    content: ContentItem[];
    received_at: number;
}

// ---- 契约一：入站 adapter ----

export interface InboundAdapter {
    // 接入握手 / 回调校验。不需要握手的 channel 返回 null。
    handleHandshake(raw: unknown): unknown | null;
    // 验签。没有签名机制的 channel 实现为恒 true（需在 adapter 内说明为何安全）。
    verify(raw: unknown): boolean;
    // 原始入站 -> 通用消息。不是要处理的消息返回 null。
    parse(raw: any): InboundMessage | null;
}

// ---- 契约四：出站 adapter ----

// adapter 只实现两个原子操作。退化逻辑不在这里——见下方 deliver()。
export interface OutboundAdapter {
    // 在指定会话里新发一条。
    send(channelChatId: string, content: string): Promise<string>;
    // 在某线程/某条消息下回复。只在确有回复语义时被 deliver 调用。
    reply(threadRef: ThreadRef, content: string): Promise<string>;
}

// 一次出站投递的目标：发去哪个会话(channelChatId)，以及可选的回复锚点。
// channelChatId 始终必须有——这正是旧 reply(threadRef) 签名缺失、导致
// 无回复语义 channel "不知道发哪" 而硬编码兜底的根因。
export interface ReplyTarget {
    channelChatId: string;
    threadRef: ThreadRef | null;
}

// 中心化出站投递：有回复锚点就走 reply，没有就退化为发到 channelChatId。
// 退化逻辑只此一处，所有 channel 复用，adapter 不再各自实现退化。
export async function deliver(
    adapter: OutboundAdapter,
    target: ReplyTarget,
    content: string,
): Promise<string> {
    if (target.threadRef !== null) {
        return adapter.reply(target.threadRef, content);
    }
    return adapter.send(target.channelChatId, content);
}

// ---- 契约三：是否需要 bot 响应 ----

// 决策刻意不是裸 boolean：不响应时必须带上 reason，让"为什么不回这条消息"
// 从契约层就可观测，直接堵死设计文档反复强调的"静默丢弃"。
export interface AddressingDecision {
    respond: boolean;
    reason: string;
}

export interface AddressingPolicy {
    decide(msg: InboundMessage, botIdentity: string): AddressingDecision;
}

// 把"不响应必须带可记录 reason"从约定变成强制。channel-server 不直接看
// decision.respond，而是过这道闸：
//   - respond=true            -> 放行，返回 true
//   - respond=false, reason 非空 -> 记一条日志，返回 false（调用方据此丢弃）
//   - respond=false, reason 空  -> 抛错。连"为什么不回"都说不出，就是设计
//     文档反复要堵的静默丢弃，必须在边界炸掉而不是无声吞消息。
export function enforceDecision(
    decision: AddressingDecision,
    log: (reason: string) => void,
): boolean {
    if (decision.respond) return true;
    if (decision.reason.trim().length === 0) {
        throw new Error(
            'AddressingDecision.respond=false but reason is empty; ' +
                'silent drop is forbidden — every skip must state why',
        );
    }
    log(decision.reason);
    return false;
}

// ---- InboundMessage 运行时守卫 ----

// adapter 是外部输入的边界，类型擦除后没人替它兜底。这个守卫让"产出了形状
// 不对的 InboundMessage"在入站时就炸，而不是流到下游才出诡异 bug。
export function assertValidInboundMessage(m: unknown): asserts m is InboundMessage {
    if (typeof m !== 'object' || m === null) {
        throw new Error('InboundMessage must be an object');
    }
    const x = m as Record<string, unknown>;
    const strFields = [
        'channel',
        'bot_name',
        'channel_message_id',
        'channel_chat_id',
        'channel_user_id',
        'conversation_scope',
    ];
    for (const f of strFields) {
        if (typeof x[f] !== 'string' || (x[f] as string).length === 0) {
            throw new Error(`InboundMessage.${f} must be a non-empty string`);
        }
    }
    if (x.thread_ref !== null && typeof x.thread_ref !== 'object') {
        throw new Error('InboundMessage.thread_ref must be ThreadRef or null');
    }
    if (!Array.isArray(x.addressing_hints)) {
        throw new Error('InboundMessage.addressing_hints must be an array');
    }
    if (!Array.isArray(x.content) || x.content.length === 0) {
        throw new Error('InboundMessage.content must be a non-empty array');
    }
    if (typeof x.received_at !== 'number') {
        throw new Error('InboundMessage.received_at must be a number');
    }
}
