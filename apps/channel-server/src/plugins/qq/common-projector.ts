// QQ common projector（QQ 插件私有职责，对飞书 common-projector）。
//
// 把 QQ openid → common_user / common_conversation / common_message（UUIDv7）的
// 投影、去重 / 复用、并发锁、入站/出站落库收进这里。读写 common_* 三表 + QQ 私有
// 三张映射表（qq_user_open_id / qq_message / qq_group_chat_info）。
//
// 身份口径（见 qq-user-open-id.ts）：私聊 user_openid 与群 member_openid 是两个
// 不同 ID 空间、member_openid 跨群变化。scope_key 把作用域折进用户主键，保证私聊
// 与群、不同群之间各自稳定归一、互不混淆。

import { v7 as uuidv7 } from 'uuid';
import AppDataSource from 'ormconfig';
import type { ContentItem, InboundMessage } from '@core/channels/contracts';
import { CommonConversation } from '@entities/common-conversation';
import { CommonMessage } from '@entities/common-message';
import { CommonUser } from '@entities/common-user';
import { QqGroupChatInfo } from '@entities/qq-group-chat-info';
import { QqMessage } from '@entities/qq-message';
import { QqUserOpenId } from '@entities/qq-user-open-id';
import { context } from '@middleware/context';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { evalScript, setNx } from '@cache/redis-client';
import { QQ_SELF_MENTION_TARGET } from './inbound';

const QQ_MESSAGE_PROJECTION_LOCK_TTL_SECONDS = 120;
const QQ_MESSAGE_PROJECTION_LOCK_TIMEOUT_MS = 60_000;
const QQ_MESSAGE_PROJECTION_LOCK_RETRY_MS = 25;
const RELEASE_LOCK_SCRIPT = `
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
`;

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

// 同一条 QQ 消息可能被 HTTP / MQ 重投并发处理。按 QQ msg_id 串行到 store 完成，
// 避免重复造 common_message。镜像飞书 withLarkInboundProjectionLock。
export async function withQqInboundProjectionLock<T>(
    qqMessageId: string,
    task: () => Promise<T>,
): Promise<T> {
    const key = `lock:qq:message-projection:${qqMessageId}`;
    const token = uuidv7();
    const deadline = Date.now() + QQ_MESSAGE_PROJECTION_LOCK_TIMEOUT_MS;

    for (;;) {
        const acquired = await setNx(key, token, QQ_MESSAGE_PROJECTION_LOCK_TTL_SECONDS);
        if (acquired === 'OK') break;
        if (Date.now() >= deadline) {
            throw new Error(`timeout acquiring qq message projection lock: ${qqMessageId}`);
        }
        await sleep(QQ_MESSAGE_PROJECTION_LOCK_RETRY_MS);
    }

    try {
        return await task();
    } finally {
        try {
            await evalScript(RELEASE_LOCK_SCRIPT, 1, key, token);
        } catch (err) {
            console.warn(
                `[qq common projector] failed to release projection lock ` +
                    `qq_message_id=${qqMessageId}: ${(err as Error).message}`,
            );
        }
    }
}

function textProjection(content: ContentItem[]): string | undefined {
    const text = content
        .map((item) => {
            if (item.kind === 'text' || item.kind === 'unsupported') return item.text;
            return `[${item.kind}]`;
        })
        .join('')
        .trim();
    return text.length > 0 ? text : undefined;
}

function primaryMessageType(content: ContentItem[]): string {
    const first = content[0];
    return first ? first.kind : 'text';
}

// 私聊：身份是 (bot, user_openid)；群聊：身份是 (bot, 群, member_openid)。
function scopeKeyFor(scope: string, conversationId: string): string {
    return scope === 'direct' ? 'direct' : `group:${conversationId}`;
}

export interface EnsureQqCommonUserInput {
    botName: string;
    scope: string;
    conversationId: string;
    openId: string;
    displayName: string | undefined;
}

export async function ensureQqCommonUser(input: EnsureQqCommonUserInput): Promise<string> {
    const repo = AppDataSource.getRepository(QqUserOpenId);
    const scopeKey = scopeKeyFor(input.scope, input.conversationId);

    // QQ 无 unionId，归一锚点只有 (bot, scopeKey, openId)。并发首投影若用「findOne 决策
    // → upsert（DO UPDATE）」会互相覆盖 commonUserId、产生孤儿 CommonUser。改成：
    //   1) 先 insert QqUserOpenId，ON CONFLICT DO NOTHING（不覆盖已有 commonUserId）；
    //   2) 读回 canonical commonUserId（先写入者赢，后到者读到同一个）；
    //   3) 用 canonical 建/更新 CommonUser（幂等，无孤儿、无覆盖）。
    const candidateCommonUserId = uuidv7();
    await repo
        .createQueryBuilder()
        .insert()
        .into(QqUserOpenId)
        .values({
            botName: input.botName,
            scopeKey,
            openId: input.openId,
            scope: input.scope,
            conversationId: input.conversationId,
            name: input.displayName ?? '',
            commonUserId: candidateCommonUserId,
        })
        .orIgnore()
        .execute();

    const linked = await repo.findOneOrFail({
        where: { botName: input.botName, scopeKey, openId: input.openId },
    });
    const canonicalCommonUserId = linked.commonUserId ?? candidateCommonUserId;

    await AppDataSource.getRepository(CommonUser).upsert(
        {
            common_user_id: canonicalCommonUserId,
            channel: 'qq',
            display_name: input.displayName,
        },
        ['common_user_id'],
    );

    return canonicalCommonUserId;
}

export interface EnsureQqCommonConversationInput {
    conversationId: string;
    scope: 'direct' | 'group' | string;
    botName: string | undefined;
    displayName?: string | undefined;
    avatarUrl?: string | undefined;
    memberCount?: number | undefined;
    isActive?: boolean;
    downloadAllowed?: boolean;
}

export async function ensureQqCommonConversation(
    input: EnsureQqCommonConversationInput,
): Promise<string> {
    const repo = AppDataSource.getRepository(QqGroupChatInfo);

    const attachmentPolicy = {
        download_allowed: input.downloadAllowed ?? true,
        source: 'qq',
    };

    // 归一锚点是 conversation_id。并发首投影若用「findOne 决策 → upsert（DO UPDATE）」
    // 会互相覆盖 common_conversation_id、产生孤儿 CommonConversation，早到的消息可能挂到
    // 被覆盖前的旧会话。镜像 ensureQqCommonUser 的收敛口径：
    //   1) 先 insert QqGroupChatInfo，ON CONFLICT DO NOTHING（不覆盖已有映射）；
    //   2) 读回 canonical common_conversation_id（先写入者赢，后到者读到同一个）；
    //   3) 用 canonical 建/刷新 CommonConversation（DO UPDATE 幂等刷新元数据，无孤儿、无覆盖）。
    const candidateCommonConversationId = uuidv7();
    await repo
        .createQueryBuilder()
        .insert()
        .into(QqGroupChatInfo)
        .values({
            conversation_id: input.conversationId,
            scope: input.scope as 'direct' | 'group',
            bot_name: input.botName,
            common_conversation_id: candidateCommonConversationId,
        })
        .orIgnore()
        .execute();

    const linked = await repo.findOneOrFail({
        where: { conversation_id: input.conversationId },
    });
    const canonicalCommonConversationId =
        linked.common_conversation_id ?? candidateCommonConversationId;

    // DO UPDATE：新会话建行、已存在会话刷新 display_name / member_count 等元数据。
    await AppDataSource.getRepository(CommonConversation).upsert(
        {
            common_conversation_id: canonicalCommonConversationId,
            channel: 'qq',
            scope: input.scope,
            display_name: input.displayName,
            avatar_url: input.avatarUrl,
            member_count: input.memberCount,
            is_active: input.isActive ?? true,
            attachment_policy: attachmentPolicy,
        },
        ['common_conversation_id'],
    );

    return canonicalCommonConversationId;
}

export async function findCommonMessageIdByQqId(qqMessageId: string): Promise<string | undefined> {
    const existing = await AppDataSource.getRepository(QqMessage).findOne({
        where: { qq_message_id: qqMessageId },
    });
    return existing?.common_message_id;
}

async function resolveQuotedCommonMessageId(
    quotedQqMessageId: string | undefined,
): Promise<string | undefined> {
    if (!quotedQqMessageId) return undefined;
    return findCommonMessageIdByQqId(quotedQqMessageId);
}

export interface QqInboundProjection {
    commonUserId: string;
    commonConversationId: string;
    commonMessageId: string;
    commonRootMessageId: string | undefined;
    commonReplyMessageId: string | undefined;
    mentionedUserIds: string[];
    content: ContentItem[];
    contentText: string | undefined;
    scope: string;
}

export async function prepareQqInboundProjection(
    inbound: InboundMessage,
    botName: string,
    botCommonUserId: string,
): Promise<QqInboundProjection> {
    const commonUserId = await ensureQqCommonUser({
        botName,
        scope: inbound.conversation_scope,
        conversationId: inbound.channel_chat_id,
        openId: inbound.channel_user_id,
        displayName: undefined,
    });

    const commonConversationId = await ensureQqCommonConversation({
        conversationId: inbound.channel_chat_id,
        scope: inbound.conversation_scope,
        botName,
    });

    const existingCommonMessageId = await findCommonMessageIdByQqId(inbound.channel_message_id);
    const commonMessageId = existingCommonMessageId ?? uuidv7();

    // QQ 没有飞书那样的话题树 root；引用回复（quote）能映射到已存在的 common 消息时
    // 作为 reply 链。root 兜底用自身。
    const commonReplyMessageId = await resolveQuotedCommonMessageId(
        inbound.thread_ref?.replyToChannelMessageId,
    );
    const commonRootMessageId = commonReplyMessageId ?? commonMessageId;

    // 只把「@ 了本 bot」折成 mention 的 common user（足够 NeedRobotMention 命中）。
    // 其它群成员 mention 不投影（本期 QQ 无依赖它的规则）。
    const mentionedUserIds = inbound.addressing_hints.some(
        (h) => h.targetId === QQ_SELF_MENTION_TARGET,
    )
        ? [botCommonUserId]
        : [];

    return {
        commonUserId,
        commonConversationId,
        commonMessageId,
        commonRootMessageId,
        commonReplyMessageId,
        mentionedUserIds,
        content: inbound.content,
        contentText: textProjection(inbound.content),
        scope: inbound.conversation_scope,
    };
}

export async function storeQqInboundMessage(
    inbound: InboundMessage,
    projection: QqInboundProjection,
    rawForAudit?: Record<string, unknown>,
): Promise<void> {
    const botName = context.getBotName() || inbound.bot_name || 'chiwei';

    await AppDataSource.transaction(async (manager) => {
        const existingQqMessage = await manager.getRepository(QqMessage).findOne({
            where: { qq_message_id: inbound.channel_message_id },
        });
        if (
            existingQqMessage &&
            existingQqMessage.common_message_id !== projection.commonMessageId
        ) {
            throw new Error(
                `qq message ${inbound.channel_message_id} already maps to ` +
                    `${existingQqMessage.common_message_id}, not ${projection.commonMessageId}`,
            );
        }

        await manager
            .createQueryBuilder()
            .insert()
            .into(CommonMessage)
            .values({
                common_message_id: projection.commonMessageId,
                channel: 'qq',
                common_conversation_id: projection.commonConversationId,
                common_user_id: projection.commonUserId,
                role: 'user',
                content: projection.content,
                content_text: projection.contentText,
                common_root_message_id: projection.commonRootMessageId,
                common_reply_message_id: projection.commonReplyMessageId,
                scope: projection.scope,
                message_type: primaryMessageType(projection.content),
                bot_name: botName,
                event_time: String(inbound.received_at),
            })
            .orIgnore()
            .execute();

        if (!existingQqMessage) {
            try {
                await manager
                    .createQueryBuilder()
                    .insert()
                    .into(QqMessage)
                    .values({
                        qq_message_id: inbound.channel_message_id,
                        common_message_id: projection.commonMessageId,
                        conversation_id: inbound.channel_chat_id,
                        bot_name: botName,
                        scope: projection.scope,
                        sender_open_id: inbound.channel_user_id,
                        reply_qq_message_id: inbound.thread_ref?.replyToChannelMessageId,
                        raw_event: rawForAudit as Record<string, unknown> | undefined as never,
                    })
                    .execute();
            } catch (err) {
                throw new Error(
                    `qq message ${inbound.channel_message_id} mapping insert failed; ` +
                        `common_message insert rolled back: ${(err as Error).message}`,
                );
            }
        }
    });
}

export async function claimQqInboundMessageForBot(input: {
    commonMessageId: string;
    botName: string;
    commonUserId: string;
}): Promise<void> {
    const result = await AppDataSource.getRepository(CommonMessage).update(
        { common_message_id: input.commonMessageId, role: 'user' },
        { bot_name: input.botName, common_user_id: input.commonUserId },
    );
    if (!result.affected) {
        throw new Error(
            `common user message ${input.commonMessageId} not found; ` +
                `cannot claim bot_name=${input.botName}`,
        );
    }
}

export interface StoreQqOutboundMessageInput {
    qqMessageId: string;
    conversationId: string;
    commonConversationId: string;
    commonRootMessageId: string | undefined;
    commonReplyMessageId: string | undefined;
    contentText: string;
    botName: string;
    scope: string;
    eventTime: number;
    messageType: string;
    responseId: string | undefined;
}

export async function storeQqOutboundMessage(input: StoreQqOutboundMessageInput): Promise<string> {
    const existing = await findCommonMessageIdByQqId(input.qqMessageId);
    const commonMessageId = existing ?? uuidv7();
    const botCommonUserId = multiBotManager.getBotCommonUserId(input.botName);

    await AppDataSource.transaction(async (manager) => {
        await manager
            .createQueryBuilder()
            .insert()
            .into(CommonMessage)
            .values({
                common_message_id: commonMessageId,
                channel: 'qq',
                common_conversation_id: input.commonConversationId,
                common_user_id: botCommonUserId,
                role: 'assistant',
                content: [{ kind: 'text', text: input.contentText }],
                content_text: input.contentText,
                common_root_message_id: input.commonRootMessageId ?? commonMessageId,
                common_reply_message_id: input.commonReplyMessageId,
                scope: input.scope,
                message_type: input.messageType,
                bot_name: input.botName,
                event_time: String(input.eventTime),
                response_id: input.responseId,
            })
            .orIgnore()
            .execute();

        await manager
            .createQueryBuilder()
            .insert()
            .into(QqMessage)
            .values({
                qq_message_id: input.qqMessageId,
                common_message_id: commonMessageId,
                conversation_id: input.conversationId,
                bot_name: input.botName,
                scope: input.scope,
            })
            .orIgnore()
            .execute();
    });

    return commonMessageId;
}
