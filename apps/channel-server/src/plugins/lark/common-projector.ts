import { v7 as uuidv7 } from 'uuid';
import AppDataSource from 'ormconfig';
import type { InboundMessage, ContentItem } from '@core/channels/contracts';
import type { Message } from '@core/models/message';
import { CommonConversation } from '@entities/common-conversation';
import { CommonMessage } from '@entities/common-message';
import { CommonUser } from '@entities/common-user';
import { LarkBaseChatInfo } from '@entities/lark-base-chat-info';
import { LarkMessage } from '@entities/lark-message';
import { LarkUserOpenId } from '@entities/lark-user-open-id';
import { context } from '@middleware/context';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { evalScript, setNx } from '@cache/redis-client';
import type { LarkMention, LarkReceiveMessage } from 'types/lark';
import {
    getCurrentLarkBotAppId,
    getLarkBotConfigByAppId,
    getLarkBotConfigByUnionId,
} from './bot-identity';

interface EnsureCommonUserInput {
    appId: string;
    openId: string;
    unionId: string | undefined;
    displayName: string | undefined;
}

export interface EnsureLarkCommonConversationInput {
    chatId: string;
    scope: string;
    displayName: string | undefined;
    avatarUrl: string | undefined;
    memberCount: number | undefined;
    isActive: boolean;
    downloadAllowed: boolean;
}

export interface LarkInboundProjection {
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

const LARK_MESSAGE_PROJECTION_LOCK_TTL_SECONDS = 120;
const LARK_MESSAGE_PROJECTION_LOCK_TIMEOUT_MS = 60_000;
const LARK_MESSAGE_PROJECTION_LOCK_RETRY_MS = 25;
const RELEASE_LOCK_SCRIPT = `
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
`;

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function withLarkInboundProjectionLock<T>(
    omId: string,
    task: () => Promise<T>,
): Promise<T> {
    const key = `lock:lark:message-projection:${omId}`;
    const token = uuidv7();
    const deadline = Date.now() + LARK_MESSAGE_PROJECTION_LOCK_TIMEOUT_MS;

    for (;;) {
        const acquired = await setNx(key, token, LARK_MESSAGE_PROJECTION_LOCK_TTL_SECONDS);
        if (acquired === 'OK') break;
        if (Date.now() >= deadline) {
            throw new Error(`timeout acquiring lark message projection lock: ${omId}`);
        }
        await sleep(LARK_MESSAGE_PROJECTION_LOCK_RETRY_MS);
    }

    try {
        return await task();
    } finally {
        try {
            await evalScript(RELEASE_LOCK_SCRIPT, 1, key, token);
        } catch (err) {
            console.warn(
                `[lark common projector] failed to release projection lock ` +
                    `om_id=${omId}: ${(err as Error).message}`,
            );
        }
    }
}

function textProjection(content: ContentItem[]): string | undefined {
    const text = content
        .map((item) => {
            if (item.kind === 'text' || item.kind === 'unsupported') {
                return item.text;
            }
            return `[${item.kind}]`;
        })
        .join('')
        .trim();
    return text.length > 0 ? text : undefined;
}

async function ensureCommonUser(input: EnsureCommonUserInput): Promise<string> {
    const larkUserRepo = AppDataSource.getRepository(LarkUserOpenId);
    const existing = await larkUserRepo.findOne({
        where: { appId: input.appId, openId: input.openId },
    });

    const existingByUnionId = input.unionId
        ? await larkUserRepo.findOne({
              where: { unionId: input.unionId },
              order: { commonUserId: 'ASC' },
          })
        : null;
    const canonicalCommonUserId = existingByUnionId?.commonUserId ?? existing?.commonUserId;
    if (canonicalCommonUserId) {
        await AppDataSource.getRepository(CommonUser).upsert(
            {
                common_user_id: canonicalCommonUserId,
                channel: 'lark',
                display_name: input.displayName,
            },
            ['common_user_id'],
        );
        await larkUserRepo.upsert(
            {
                appId: input.appId,
                openId: input.openId,
                unionId: input.unionId ?? existing?.unionId,
                name: input.displayName ?? existing?.name ?? '',
                commonUserId: canonicalCommonUserId,
            },
            ['appId', 'openId'],
        );
        return canonicalCommonUserId;
    }

    const commonUserId = uuidv7();
    await AppDataSource.getRepository(CommonUser).upsert(
        {
            common_user_id: commonUserId,
            channel: 'lark',
            display_name: input.displayName,
        },
        ['common_user_id'],
    );
    await larkUserRepo.upsert(
        {
            appId: input.appId,
            openId: input.openId,
            unionId: input.unionId,
            name: input.displayName ?? '',
            commonUserId,
        },
        ['appId', 'openId'],
    );

    const linked = await larkUserRepo.findOneOrFail({
        where: { appId: input.appId, openId: input.openId },
    });
    return linked.commonUserId ?? commonUserId;
}

function commonUserIdForRegisteredBot(mention: LarkMention): string | undefined {
    const byAppId = mention.bot_info?.app_id
        ? getLarkBotConfigByAppId(mention.bot_info.app_id)
        : null;
    const byUnionId = mention.id.union_id ? getLarkBotConfigByUnionId(mention.id.union_id) : null;
    const bot = byAppId ?? byUnionId;
    if (!bot) return undefined;
    if (!bot.common_user_id) {
        throw new Error(
            `registered bot mention "${bot.bot_name}" has no common_user_id; ` +
                'bot identity initialization must run before Lark mention projection',
        );
    }
    return bot.common_user_id;
}

export async function projectLarkMentionedCommonUserIds(
    appId: string,
    mentions: LarkMention[],
): Promise<string[]> {
    const out: string[] = [];
    const seen = new Set<string>();
    for (const mention of mentions) {
        let commonUserId = commonUserIdForRegisteredBot(mention);
        if (!commonUserId) {
            const openId = mention.id.open_id;
            if (!openId) {
                throw new Error(
                    `lark mention "${mention.name}" has no open_id and is not a ` +
                        'registered bot; cannot map mention to common_user',
                );
            }
            commonUserId = await ensureCommonUser({
                appId,
                openId,
                unionId: mention.id.union_id,
                displayName: mention.name,
            });
        }
        if (!seen.has(commonUserId)) {
            seen.add(commonUserId);
            out.push(commonUserId);
        }
    }
    return out;
}

export async function ensureLarkCommonConversation(
    input: EnsureLarkCommonConversationInput,
): Promise<string> {
    const larkChatRepo = AppDataSource.getRepository(LarkBaseChatInfo);
    const existing = await larkChatRepo.findOne({
        where: { chat_id: input.chatId },
    });
    if (existing?.common_conversation_id) {
        await AppDataSource.getRepository(CommonConversation).update(
            { common_conversation_id: existing.common_conversation_id },
            {
                display_name: input.displayName,
                avatar_url: input.avatarUrl,
                member_count: input.memberCount,
                is_active: input.isActive,
                attachment_policy: {
                    download_allowed: input.downloadAllowed,
                    source: 'lark',
                },
            },
        );
        return existing.common_conversation_id;
    }

    const commonConversationId = uuidv7();
    await AppDataSource.getRepository(CommonConversation).upsert(
        {
            common_conversation_id: commonConversationId,
            channel: 'lark',
            scope: input.scope,
            display_name: input.displayName,
            avatar_url: input.avatarUrl,
            member_count: input.memberCount,
            is_active: input.isActive,
            attachment_policy: {
                download_allowed: input.downloadAllowed,
                source: 'lark',
            },
        },
        ['common_conversation_id'],
    );
    await larkChatRepo.upsert(
        {
            chat_id: input.chatId,
            chat_mode: input.scope === 'direct' ? 'p2p' : 'group',
            common_conversation_id: commonConversationId,
        },
        ['chat_id'],
    );

    const linked = await larkChatRepo.findOneOrFail({
        where: { chat_id: input.chatId },
    });
    return linked.common_conversation_id ?? commonConversationId;
}

async function findCommonMessageIdByOmId(omId: string): Promise<string | undefined> {
    const existing = await AppDataSource.getRepository(LarkMessage).findOne({
        where: { om_id: omId },
    });
    return existing?.common_message_id;
}

async function resolveReferencedMessage(
    omId: string | undefined,
    selfOmId: string,
    selfCommonMessageId: string,
    which: string,
): Promise<string | undefined> {
    if (!omId) return undefined;
    if (omId === selfOmId) return selfCommonMessageId;
    const existing = await findCommonMessageIdByOmId(omId);
    if (!existing) {
        throw new Error(
            `lark ${which} message ${omId} has no common mapping; ` +
                'historical backfill must run before runtime cutover',
        );
    }
    return existing;
}

export async function prepareLarkInboundProjection(
    event: LarkReceiveMessage,
    message: Message,
    inbound: InboundMessage,
): Promise<LarkInboundProjection> {
    const appId = event.app_id || getCurrentLarkBotAppId();
    const openId = event.sender.sender_id?.open_id;
    if (!openId) {
        throw new Error('lark inbound sender open_id missing; cannot map common_user');
    }

    const commonUserId = await ensureCommonUser({
        appId,
        openId,
        unionId: event.sender.sender_id?.union_id,
        displayName: message.senderInfo?.name,
    });
    const mentionedUserIds = await projectLarkMentionedCommonUserIds(
        appId,
        event.message.mentions ?? [],
    );

    const commonConversationId = await ensureLarkCommonConversation({
        chatId: event.message.chat_id,
        scope: inbound.conversation_scope,
        displayName: message.isP2P() ? message.senderInfo?.name : message.groupChatInfo?.name,
        avatarUrl: message.isP2P()
            ? message.senderInfo?.avatar_origin
            : message.groupChatInfo?.avatar,
        memberCount: message.groupChatInfo?.user_count,
        isActive: !message.groupChatInfo?.is_leave,
        downloadAllowed: message.allowDownloadResource(),
    });

    const existingCommonMessageId = await findCommonMessageIdByOmId(event.message.message_id);
    const commonMessageId = existingCommonMessageId ?? uuidv7();
    const commonRootMessageId =
        (await resolveReferencedMessage(
            event.message.root_id,
            event.message.message_id,
            commonMessageId,
            'root',
        )) ?? commonMessageId;
    const commonReplyMessageId = await resolveReferencedMessage(
        event.message.parent_id,
        event.message.message_id,
        commonMessageId,
        'parent',
    );

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

export async function storeLarkInboundMessage(
    event: LarkReceiveMessage,
    projection: LarkInboundProjection,
    message: Message,
): Promise<void> {
    const botName = context.getBotName() || 'chiwei';
    let inserted = false;

    await AppDataSource.transaction(async (manager) => {
        const existingLarkMessage = await manager.getRepository(LarkMessage).findOne({
            where: { om_id: event.message.message_id },
        });
        if (
            existingLarkMessage &&
            existingLarkMessage.common_message_id !== projection.commonMessageId
        ) {
            throw new Error(
                `lark message ${event.message.message_id} already maps to ` +
                    `${existingLarkMessage.common_message_id}, not ` +
                    `${projection.commonMessageId}`,
            );
        }

        const insertResult = await manager
            .createQueryBuilder()
            .insert()
            .into(CommonMessage)
            .values({
                common_message_id: projection.commonMessageId,
                channel: 'lark',
                common_conversation_id: projection.commonConversationId,
                common_user_id: projection.commonUserId,
                sender_display_name: message.senderInfo?.name,
                role: 'user',
                content: projection.content,
                content_text: projection.contentText,
                common_root_message_id: projection.commonRootMessageId,
                common_reply_message_id: projection.commonReplyMessageId,
                scope: projection.scope,
                message_type: event.message.message_type,
                bot_name: botName,
                event_time: event.message.create_time,
            })
            .orIgnore()
            .execute();
        inserted = insertResult.identifiers.length > 0;

        if (!existingLarkMessage) {
            try {
                await manager
                    .createQueryBuilder()
                    .insert()
                    .into(LarkMessage)
                    .values({
                        om_id: event.message.message_id,
                        common_message_id: projection.commonMessageId,
                        chat_id: event.message.chat_id,
                        sender_open_id: event.sender.sender_id?.open_id,
                        sender_union_id: event.sender.sender_id?.union_id,
                        root_om_id: event.message.root_id,
                        reply_om_id: event.message.parent_id,
                        message_type: event.message.message_type,
                        raw_event: event as any,
                    })
                    .execute();
            } catch (err) {
                throw new Error(
                    `lark message ${event.message.message_id} mapping insert failed; ` +
                        `common_message insert rolled back: ${(err as Error).message}`,
                );
            }
        }
    });

}

export async function claimLarkInboundMessageForBot(input: {
    commonMessageId: string;
    botName: string;
    commonUserId: string;
}): Promise<void> {
    const result = await AppDataSource.getRepository(CommonMessage).update(
        {
            common_message_id: input.commonMessageId,
            role: 'user',
        },
        {
            bot_name: input.botName,
            common_user_id: input.commonUserId,
        },
    );
    if (!result.affected) {
        throw new Error(
            `common user message ${input.commonMessageId} not found; ` +
                `cannot claim bot_name=${input.botName}`,
        );
    }
}

export interface StoreLarkOutboundMessageInput {
    omId: string;
    chatId: string;
    commonConversationId: string;
    commonRootMessageId: string | undefined;
    commonReplyMessageId: string | undefined;
    contentText: string;
    botName: string;
    senderDisplayName: string | undefined;
    scope: string;
    eventTime: number;
    messageType: string;
    responseId: string | undefined;
}

export async function storeLarkOutboundMessage(
    input: StoreLarkOutboundMessageInput,
): Promise<string> {
    const existing = await findCommonMessageIdByOmId(input.omId);
    const commonMessageId = existing ?? uuidv7();
    const botCommonUserId = multiBotManager.getBotCommonUserId(input.botName);
    let inserted = false;

    await AppDataSource.transaction(async (manager) => {
        const insertResult = await manager
            .createQueryBuilder()
            .insert()
            .into(CommonMessage)
            .values({
                common_message_id: commonMessageId,
                channel: 'lark',
                common_conversation_id: input.commonConversationId,
                common_user_id: botCommonUserId,
                sender_display_name: input.senderDisplayName,
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
        inserted = insertResult.identifiers.length > 0;

        await manager
            .createQueryBuilder()
            .insert()
            .into(LarkMessage)
            .values({
                om_id: input.omId,
                common_message_id: commonMessageId,
                chat_id: input.chatId,
                message_type: input.messageType,
            })
            .orIgnore()
            .execute();
    });

    return commonMessageId;
}
