// channel 接入的四层契约。这不是"飞书+QQ 的最小公倍数"，是通用接入契约：
// 任何能把消息送进来、把回复送回去的接入点都是一个 channel。契约里刻意不出现
// @ / 私聊群聊二元 / 回复树这些 IM 专有概念作为强制语义——它们只能活在各
// channel 自己的 adapter 内部。判断字段能否进契约的唯一标准：换一个非 IM
// channel（如纯 HTTP 问答入口）它是否还讲得通。

// ---- 内容 ----

// 一条消息可由多个内容片段组成（如富文本里文字+图片交替）。kind 是跨 channel
// 都讲得通的"通用媒体语义"，不是某个 IM 的消息类型枚举：任何 channel 把它的
// 原生类型映射到这几类里，渠道专有结构/字段名只能留在各自 adapter 内，不上浮
// 到契约层。
//   text        纯文字
//   image       图片，key 是 channel 内可解析回原图的引用
//   audio       语音，key 是音频引用；meta 可带 duration 等
//   file        文件/视频等"可下载附件"，key 是附件引用；meta 可带 file_name 等
//   sticker     表情包，key 是表情引用
//   mention     被提及对象，id 是 channel adapter 已知的稳定身份 id，label 仅用于展示；
//               meta 可放 open_id/user_id/union_id/mentioned_type 等 channel 原生补充字段。
//   unsupported channel 能识别但本通道不渲染的类型；text 是给人看的占位串，
//               meta.original_type 保留原类型名，保证"收到了但没处理"可观测，
//               堵死静默丢弃。
export type ContentItem =
    | { kind: 'text'; text: string }
    | { kind: 'mention'; id: string; label: string; meta?: Record<string, unknown> }
    | { kind: 'image'; key: string; meta?: Record<string, unknown> }
    | { kind: 'audio'; key: string; meta?: Record<string, unknown> }
    | { kind: 'file'; key: string; meta?: Record<string, unknown> }
    | { kind: 'sticker'; key: string; meta?: Record<string, unknown> }
    | { kind: 'unsupported'; text: string; meta?: Record<string, unknown> };

// ---- 线程 / 关联引用 ----

// "这条回复挂在哪"的通用锚点集合；没有回复语义的 channel 直接传 null。
//   selfChannelMessageId 触发本次处理的那条消息自身的 id —— "回复用户刚发的
//                        这条" 这个最常见锚点。缺它时 IM 回复会从"回复原消息"
//                        退化成顶层发送。
//   replyToChannelMessageId / rootChannelMessageId 回复树上游/根（可选）。
//   inThread 这次回复是否要留在同一话题串内。是否真有"话题"概念由各 channel
//            决定；非线程 channel 不设即可（视作 false）。这字段刻意是通用的
//            "保持在同一会话串"语义，不绑定任何 IM 的 thread 实现。
export interface ThreadRef {
    selfChannelMessageId?: string;
    replyToChannelMessageId?: string;
    rootChannelMessageId?: string;
    inThread?: boolean;
}

// ---- 寻址线索 ----

// channel 给的"这条消息冲谁来"的线索。IM 里就是被 @ 的对象；HTTP 入口可能为空。
// 由 AddressingPolicy 解释，契约不假设它一定是 @。
export interface AddressingHint {
    targetId: string;
}

// ---- 通用入站消息 ----

// 所有 InboundAdapter.parse 都产出这同一个结构。身份字段带 channel_ 前缀，
// 表示是"channel 内的 ID"；各插件接线点负责把它投影到 common_*。
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

// ---- 契约三：是否需要 bot 响应 ----

// 决策刻意不是裸 boolean：不响应时必须带上 reason，让"为什么不回这条消息"
// 从契约层就可观测，直接堵死设计文档反复强调的"静默丢弃"。
export interface AddressingDecision {
    respond: boolean;
    reason: string;
}

// botMentionTarget 契约约束（跨 channel 通用，不含任何渠道专有命名）：
// 调用方传入的 botMentionTarget 必须与该 channel 的 AddressingHint.targetId 处在
// 同一 ID 空间——decide 是靠 hint.targetId === botMentionTarget 判断"这条冲 bot 来"。
// 每个 channel 的 InboundAdapter 自己决定 targetId 用哪种 ID 口径，调用方就
// 必须按同口径取 bot 标识。传错 ID 空间会让 @bot 永不命中、bot 静默不响应。
// 各 channel 的具体 ID 口径写在该 channel 自己的 adapter 内部注释 + 等价性
// 测试里，契约层不感知（见各 channel 插件的入站/寻址测试，如
// plugins/lark/inbound.test.ts、plugins/lark/addressing.test.ts）。
export interface AddressingPolicy {
    decide(msg: InboundMessage, botMentionTarget: string): AddressingDecision;
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
    if (x.thread_ref !== null) {
        if (typeof x.thread_ref !== 'object') {
            throw new Error('InboundMessage.thread_ref must be ThreadRef or null');
        }
        assertThreadRefHasAnchor(x.thread_ref as Record<string, unknown>);
    }
    if (!Array.isArray(x.addressing_hints)) {
        throw new Error('InboundMessage.addressing_hints must be an array');
    }
    if (!Array.isArray(x.content) || x.content.length === 0) {
        throw new Error('InboundMessage.content must be a non-empty array');
    }
    for (const item of x.content as unknown[]) {
        assertValidContentItem(item);
    }
    if (typeof x.received_at !== 'number') {
        throw new Error('InboundMessage.received_at must be a number');
    }
}

// 一个非 null 的 ThreadRef 必须至少带一个非空字符串锚点（self/replyTo/root
// 任一）。inThread 只是"是否留在同一话题串"的布尔修饰，不是锚点——光有它
// 出站 reply 会把回复目标解析成空字符串再去调 channel reply，违反设计文档
//"禁止静默丢弃"。空锚点必须在入站边界炸，而不是无声发到错地方。
function assertThreadRefHasAnchor(tr: Record<string, unknown>): void {
    const anchorFields = [
        'selfChannelMessageId',
        'replyToChannelMessageId',
        'rootChannelMessageId',
    ];
    const hasAnchor = anchorFields.some(
        (f) => typeof tr[f] === 'string' && (tr[f] as string).length > 0,
    );
    if (!hasAnchor) {
        throw new Error(
            'InboundMessage.thread_ref is non-null but carries no usable anchor; ' +
                'at least one of selfChannelMessageId/replyToChannelMessageId/' +
                'rootChannelMessageId must be a non-empty string (use thread_ref=null ' +
                'when there is no reply semantics) — silent drop is forbidden',
        );
    }
}

// 单个内容片段的形状守卫：text/unsupported 必须有非空 text，其余媒体类必须有
// 非空 key。形状不对就在入站边界炸，而不是流到下游才出诡异 bug。
function assertValidContentItem(item: unknown): asserts item is ContentItem {
    if (typeof item !== 'object' || item === null) {
        throw new Error('ContentItem must be an object');
    }
    const it = item as Record<string, unknown>;
    switch (it.kind) {
        case 'text':
        case 'unsupported':
            if (typeof it.text !== 'string' || (it.text as string).length === 0) {
                throw new Error(`ContentItem(${it.kind}).text must be a non-empty string`);
            }
            return;
        case 'mention':
            if (typeof it.id !== 'string' || (it.id as string).length === 0) {
                throw new Error('ContentItem(mention).id must be a non-empty string');
            }
            if (typeof it.label !== 'string' || (it.label as string).length === 0) {
                throw new Error('ContentItem(mention).label must be a non-empty string');
            }
            return;
        case 'image':
        case 'audio':
        case 'file':
        case 'sticker':
            if (typeof it.key !== 'string' || (it.key as string).length === 0) {
                throw new Error(`ContentItem(${it.kind}).key must be a non-empty string`);
            }
            return;
        default:
            throw new Error(`ContentItem.kind is not a recognized kind: ${String(it.kind)}`);
    }
}
