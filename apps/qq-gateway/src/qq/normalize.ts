/**
 * QQ 原始 webhook 事件 → 平台无关的 CustomInboundMessage 归一化。
 *
 * 字段提取规则移植自 openclaw-qqbot/src/gateway.ts 的事件分发与 src/types.ts 的事件结构，
 * 但只保留「被动收文本」所需的私聊（C2C）与群 @（GROUP_AT）两类，且不引入 openclaw 依赖。
 *
 * - C2C_MESSAGE_CREATE              → chatType=direct
 * - GROUP_AT_MESSAGE_CREATE /
 *   GROUP_MESSAGE_CREATE            → chatType=group（保留 mentions + isSelf，交给 channel-server 做 @bot 门控）
 * - 其它（GROUP_ADD_ROBOT 等系统事件）→ 返回 null，不转发
 *
 * 群消息的 @bot 文本（<@member_openid>）保持原样透传，是否剥离 / 是否唤起由 channel-server 决定。
 */

import type { CustomInboundAttachment, CustomInboundMention, CustomInboundMessage } from '@inner/shared/protocols';

export interface NormalizeContext {
    /** 收到此消息的 bot 名，channel-server 据此查 bot 配置。 */
    botName: string;
}

/** QQ 富媒体附件原始结构（src/types.ts MessageAttachment）。 */
interface RawAttachment {
    content_type?: string;
    url?: string;
    filename?: string;
    size?: number;
    width?: number;
    height?: number;
    voice_wav_url?: string;
    asr_refer_text?: string;
}

/** QQ 群 @ 提及原始结构（src/types.ts GroupMessageEvent.mentions）。 */
interface RawMention {
    scope?: 'all' | 'single';
    id?: string;
    user_openid?: string;
    member_openid?: string;
    nickname?: string;
    username?: string;
    bot?: boolean;
    is_you?: boolean;
}

interface RawC2CEvent {
    author?: { id?: string; union_openid?: string; user_openid?: string };
    content?: string;
    id?: string;
    timestamp?: string | number;
    attachments?: RawAttachment[];
}

interface RawGroupEvent {
    author?: { id?: string; member_openid?: string; username?: string; bot?: boolean };
    content?: string;
    id?: string;
    timestamp?: string | number;
    group_openid?: string;
    mentions?: RawMention[];
    attachments?: RawAttachment[];
}

const GROUP_EVENT_TYPES = new Set(['GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE']);

/** 时间戳归一化为 ISO 字符串。QQ c2c/group 给的是 RFC3339 字符串，直接透传；数字按毫秒/秒转。 */
function toIso(ts: string | number | undefined): string {
    if (typeof ts === 'string' && ts.length > 0) return ts;
    if (typeof ts === 'number' && Number.isFinite(ts)) {
        const ms = ts > 1e12 ? ts : ts * 1000; // 秒 vs 毫秒启发式
        return new Date(ms).toISOString();
    }
    return new Date().toISOString();
}

function mapAttachments(raw: RawAttachment[] | undefined): CustomInboundAttachment[] | undefined {
    if (!raw || raw.length === 0) return undefined;
    // 空 url 的附件直接丢弃：空 key 流进识图 / 落库会被合成兜底污染下游，且 wire 守卫也会拒收。
    const mapped = raw
        .filter((a) => typeof a.url === 'string' && a.url.length > 0)
        .map((a) => {
            const att: CustomInboundAttachment = {
                contentType: a.content_type ?? 'application/octet-stream',
                url: a.url as string,
            };
            if (a.filename !== undefined) att.filename = a.filename;
            if (a.size !== undefined) att.size = a.size;
            if (a.width !== undefined) att.width = a.width;
            if (a.height !== undefined) att.height = a.height;
            if (a.voice_wav_url !== undefined) att.voiceWavUrl = a.voice_wav_url;
            if (a.asr_refer_text !== undefined) att.asrText = a.asr_refer_text;
            return att;
        });
    return mapped.length > 0 ? mapped : undefined;
}

function mapMentions(raw: RawMention[] | undefined): CustomInboundMention[] | undefined {
    if (!raw || raw.length === 0) return undefined;
    return raw.map((m) => {
        const mention: CustomInboundMention = {};
        if (m.id !== undefined) mention.id = m.id;
        if (m.user_openid !== undefined) mention.userId = m.user_openid;
        if (m.member_openid !== undefined) mention.memberId = m.member_openid;
        const name = m.nickname ?? m.username;
        if (name !== undefined) mention.name = name;
        if (m.bot !== undefined) mention.isBot = m.bot;
        // is_you = QQ 标记的 "@了机器人自己"，群聊唤起判定的关键字段
        mention.isSelf = m.is_you === true;
        return mention;
    });
}

function normalizeC2C(d: RawC2CEvent, ctx: NormalizeContext): CustomInboundMessage | null {
    // 缺必要裸 id（发送者 user_openid / messageId）就丢弃：填 "unknown" 会把坏事件
    // 投影成真实 common_user / conversation，污染身份与会话。fail-loud = 返回 null。
    const senderId = d.author?.user_openid;
    const messageId = d.id;
    if (!senderId || !messageId) return null;

    const msg: CustomInboundMessage = {
        botName: ctx.botName,
        chatType: 'direct',
        // 私聊会话即用户本身，会话 id == 用户 openid
        conversationId: senderId,
        senderId,
        text: d.content ?? '',
        messageId,
        timestamp: toIso(d.timestamp),
        raw: d,
    };
    const attachments = mapAttachments(d.attachments);
    if (attachments) msg.attachments = attachments;
    return msg;
}

function normalizeGroup(d: RawGroupEvent, ctx: NormalizeContext): CustomInboundMessage | null {
    // 缺必要裸 id（群 group_openid / 发送者 member_openid / messageId）就丢弃，理由同上。
    const conversationId = d.group_openid;
    const senderId = d.author?.member_openid;
    const messageId = d.id;
    if (!conversationId || !senderId || !messageId) return null;

    const msg: CustomInboundMessage = {
        botName: ctx.botName,
        chatType: 'group',
        conversationId,
        senderId,
        text: d.content ?? '',
        messageId,
        timestamp: toIso(d.timestamp),
        raw: d,
    };
    if (d.author?.username !== undefined) msg.senderName = d.author.username;
    if (d.author?.bot !== undefined) msg.senderIsBot = d.author.bot;
    const mentions = mapMentions(d.mentions);
    if (mentions) msg.mentions = mentions;
    const attachments = mapAttachments(d.attachments);
    if (attachments) msg.attachments = attachments;
    return msg;
}

/**
 * 把一个 QQ webhook 事件归一化为 CustomInboundMessage。
 * 返回 null 表示该事件不属于「被动收文本」范畴（系统事件 / 未支持类型），不应转发。
 */
export function normalizeQQEvent(eventType: string, d: unknown, ctx: NormalizeContext): CustomInboundMessage | null {
    if (typeof d !== 'object' || d === null) return null;
    if (eventType === 'C2C_MESSAGE_CREATE') {
        return normalizeC2C(d as RawC2CEvent, ctx);
    }
    if (GROUP_EVENT_TYPES.has(eventType)) {
        return normalizeGroup(d as RawGroupEvent, ctx);
    }
    return null;
}
