import type {
    CustomChatType,
    CustomInboundAttachment,
    CustomInboundMention,
    CustomInboundMessage,
    CustomInboundQuote,
    CustomOutboundMessage,
    CustomOutboundResult,
} from './types';

/**
 * Wire 边界守卫：运行时校验入站 / 出站消息。
 *
 * JSON 过网时不带类型，TS 类型只在编译期成立，所以网关产的消息进 channel-server、
 * channel-server 产的消息进网关时都必须过这里。校验失败 fail-loud（throw 描述性错误），
 * 不静默吞掉、不返回半成品。
 */

const CHAT_TYPES: readonly CustomChatType[] = ['direct', 'group'];

function isPlainObject(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** 校验一个必填字段为字符串（允许空串）。 */
function requireString(obj: Record<string, unknown>, field: string, ctx: string): string {
    const value = obj[field];
    if (typeof value !== 'string') {
        throw new Error(`${ctx}: field "${field}" must be a string, got ${describe(value)}`);
    }
    return value;
}

/** 校验一个必填字段为非空字符串（空串视为缺失，fail-loud）。 */
function requireNonEmptyString(obj: Record<string, unknown>, field: string, ctx: string): string {
    const value = obj[field];
    if (typeof value !== 'string') {
        throw new Error(`${ctx}: field "${field}" must be a string, got ${describe(value)}`);
    }
    if (value.length === 0) {
        throw new Error(`${ctx}: field "${field}" must be a non-empty string`);
    }
    return value;
}

/** 校验一个可选字符串字段；不存在返回 undefined。 */
function optionalString(obj: Record<string, unknown>, field: string, ctx: string): string | undefined {
    const value = obj[field];
    if (value === undefined) return undefined;
    if (typeof value !== 'string') {
        throw new Error(`${ctx}: optional field "${field}" must be a string when present, got ${describe(value)}`);
    }
    return value;
}

function optionalNumber(obj: Record<string, unknown>, field: string, ctx: string): number | undefined {
    const value = obj[field];
    if (value === undefined) return undefined;
    if (typeof value !== 'number' || Number.isNaN(value)) {
        throw new Error(`${ctx}: optional field "${field}" must be a number when present, got ${describe(value)}`);
    }
    return value;
}

function optionalBoolean(obj: Record<string, unknown>, field: string, ctx: string): boolean | undefined {
    const value = obj[field];
    if (value === undefined) return undefined;
    if (typeof value !== 'boolean') {
        throw new Error(`${ctx}: optional field "${field}" must be a boolean when present, got ${describe(value)}`);
    }
    return value;
}

function requireBoolean(obj: Record<string, unknown>, field: string, ctx: string): boolean {
    const value = obj[field];
    if (typeof value !== 'boolean') {
        throw new Error(`${ctx}: field "${field}" must be a boolean, got ${describe(value)}`);
    }
    return value;
}

function requireChatType(obj: Record<string, unknown>, ctx: string): CustomChatType {
    const value = obj['chatType'];
    if (typeof value !== 'string' || !CHAT_TYPES.includes(value as CustomChatType)) {
        throw new Error(`${ctx}: field "chatType" must be one of ${CHAT_TYPES.join(' | ')}, got ${describe(value)}`);
    }
    return value as CustomChatType;
}

function describe(value: unknown): string {
    if (value === null) return 'null';
    if (Array.isArray(value)) return 'array';
    return typeof value;
}

function validateAttachment(value: unknown, ctx: string): CustomInboundAttachment {
    if (!isPlainObject(value)) {
        throw new Error(`${ctx}: attachment must be an object, got ${describe(value)}`);
    }
    return {
        contentType: requireString(value, 'contentType', ctx),
        // url 必须非空：空 url 会让识图 / 落库拿到空 key（合成兜底污染下游），fail-loud。
        url: requireNonEmptyString(value, 'url', ctx),
        filename: optionalString(value, 'filename', ctx),
        size: optionalNumber(value, 'size', ctx),
        width: optionalNumber(value, 'width', ctx),
        height: optionalNumber(value, 'height', ctx),
        voiceWavUrl: optionalString(value, 'voiceWavUrl', ctx),
        asrText: optionalString(value, 'asrText', ctx),
    };
}

function validateAttachments(
    obj: Record<string, unknown>,
    field: string,
    ctx: string,
): CustomInboundAttachment[] | undefined {
    const value = obj[field];
    if (value === undefined) return undefined;
    if (!Array.isArray(value)) {
        throw new Error(`${ctx}: optional field "${field}" must be an array when present, got ${describe(value)}`);
    }
    return value.map((item, i) => validateAttachment(item, `${ctx}.${field}[${i}]`));
}

function validateMention(value: unknown, ctx: string): CustomInboundMention {
    if (!isPlainObject(value)) {
        throw new Error(`${ctx}: mention must be an object, got ${describe(value)}`);
    }
    return {
        id: optionalString(value, 'id', ctx),
        userId: optionalString(value, 'userId', ctx),
        memberId: optionalString(value, 'memberId', ctx),
        name: optionalString(value, 'name', ctx),
        isBot: optionalBoolean(value, 'isBot', ctx),
        isSelf: optionalBoolean(value, 'isSelf', ctx),
    };
}

function validateMentions(obj: Record<string, unknown>, ctx: string): CustomInboundMention[] | undefined {
    const value = obj['mentions'];
    if (value === undefined) return undefined;
    if (!Array.isArray(value)) {
        throw new Error(`${ctx}: optional field "mentions" must be an array when present, got ${describe(value)}`);
    }
    return value.map((item, i) => validateMention(item, `${ctx}.mentions[${i}]`));
}

function validateQuote(obj: Record<string, unknown>, ctx: string): CustomInboundQuote | undefined {
    const value = obj['quote'];
    if (value === undefined) return undefined;
    if (!isPlainObject(value)) {
        throw new Error(`${ctx}: optional field "quote" must be an object when present, got ${describe(value)}`);
    }
    const quoteCtx = `${ctx}.quote`;
    return {
        refId: optionalString(value, 'refId', quoteCtx),
        messageId: optionalString(value, 'messageId', quoteCtx),
        content: optionalString(value, 'content', quoteCtx),
        senderId: optionalString(value, 'senderId', quoteCtx),
        senderName: optionalString(value, 'senderName', quoteCtx),
        attachments: validateAttachments(value, 'attachments', quoteCtx),
    };
}

function validateStringArray(
    obj: Record<string, unknown>,
    field: string,
    ctx: string,
): string[] | undefined {
    const value = obj[field];
    if (value === undefined) return undefined;
    if (!Array.isArray(value)) {
        throw new Error(`${ctx}: optional field "${field}" must be an array when present, got ${describe(value)}`);
    }
    value.forEach((item, i) => {
        if (typeof item !== 'string') {
            throw new Error(`${ctx}: field "${field}[${i}]" must be a string, got ${describe(item)}`);
        }
    });
    return value as string[];
}

/**
 * 校验网关推来的入站消息。缺必填字段 / 类型错抛描述性错误。
 * 通过则返回归一化后的消息（结构等价于入参）。
 */
export function validateCustomInboundMessage(input: unknown): CustomInboundMessage {
    const ctx = 'CustomInboundMessage';
    if (!isPlainObject(input)) {
        throw new Error(`${ctx}: payload must be an object, got ${describe(input)}`);
    }
    const msg: CustomInboundMessage = {
        botName: requireString(input, 'botName', ctx),
        chatType: requireChatType(input, ctx),
        conversationId: requireString(input, 'conversationId', ctx),
        senderId: requireString(input, 'senderId', ctx),
        senderName: optionalString(input, 'senderName', ctx),
        senderIsBot: optionalBoolean(input, 'senderIsBot', ctx),
        text: requireString(input, 'text', ctx),
        messageId: requireString(input, 'messageId', ctx),
        timestamp: requireString(input, 'timestamp', ctx),
        attachments: validateAttachments(input, 'attachments', ctx),
        mentions: validateMentions(input, ctx),
        quote: validateQuote(input, ctx),
    };
    if ('raw' in input) {
        msg.raw = input['raw'];
    }
    return msg;
}

/**
 * 校验 channel-server 发来的出站消息。缺必填字段 / 类型错抛描述性错误。
 * 通过则返回归一化后的消息（结构等价于入参）。
 */
export function validateCustomOutboundMessage(input: unknown): CustomOutboundMessage {
    const ctx = 'CustomOutboundMessage';
    if (!isPlainObject(input)) {
        throw new Error(`${ctx}: payload must be an object, got ${describe(input)}`);
    }
    const msg: CustomOutboundMessage = {
        botName: requireString(input, 'botName', ctx),
        chatType: requireChatType(input, ctx),
        conversationId: requireString(input, 'conversationId', ctx),
        replyToMessageId: optionalString(input, 'replyToMessageId', ctx),
        text: optionalString(input, 'text', ctx),
        mediaUrls: validateStringArray(input, 'mediaUrls', ctx),
        partIndex: optionalNumber(input, 'partIndex', ctx),
        isLast: optionalBoolean(input, 'isLast', ctx),
        idempotencyKey: requireString(input, 'idempotencyKey', ctx),
    };
    if ('raw' in input) {
        msg.raw = input['raw'];
    }
    return msg;
}

/**
 * 校验网关回执给 channel-server 的出站发送结果。缺 sent / 类型错抛描述性错误。
 * 通过则返回归一化后的结果（结构等价于入参）。
 */
export function validateCustomOutboundResult(input: unknown): CustomOutboundResult {
    const ctx = 'CustomOutboundResult';
    if (!isPlainObject(input)) {
        throw new Error(`${ctx}: payload must be an object, got ${describe(input)}`);
    }
    const sent = requireBoolean(input, 'sent', ctx);
    return {
        sent,
        // sent=true 必须带非空 messageId（落库 / 续段锚点用它）：空 messageId 会让 worker 合成
        // 兜底 id 落库、污染 qq_message。sent=false 时 messageId 可省略，只给 reason。
        messageId: sent
            ? requireNonEmptyString(input, 'messageId', ctx)
            : optionalString(input, 'messageId', ctx),
        reason: optionalString(input, 'reason', ctx),
    };
}
