// 被 @ 的人叫什么名字。
//
// 这是解析里唯一需要**我们自己的知识**的一步：飞书只会说"union_id 是 on_xxx 的
// 那位"，而群里读到的应该是"@赤尾"，也就是人设名。人设名在 bot 目录里，所以这
// 一步跟 parse-message 分开 —— 解析纯到不需要任何桩，起名字才带依赖。
//
// 依赖以端口形式注入（LarkBotLookup），本文件不认识 BotDirectory、不认识
// credentials 的形状。

import type { LarkMention } from './wire';

/** 一个我们自己在跑的飞书 bot。 */
export interface LarkBotIdentity {
    botName: string;
    /** 人设展示名。没绑人设的工具 bot 是 null。 */
    displayName: string | null;
    /** 身份初始化后才有。缺失说明启动序列没跑完，见下方 fail-loud。 */
    commonUserId?: string;
}

export interface LarkBotLookup {
    byAppId(appId: string): LarkBotIdentity | null;
    byUnionId(unionId: string): LarkBotIdentity | null;
}

export interface ResolvedLarkMention {
    /** 正文里对应的 `@_user_N` 占位符。 */
    token: string;
    unionId?: string;
    displayName: string;
    /** 被 @ 的是我们自己的 bot 时才有。用来判断"这条是不是冲我来的"。 */
    botCommonUserId?: string;
}

export interface LarkMentionIndex {
    readonly all: readonly ResolvedLarkMention[];
    byToken(token: string): ResolvedLarkMention | undefined;
}

/**
 * 只有 `mentioned_type === 'bot'` 才值得查目录。飞书优先给 bot_info.app_id，
 * 老一点的事件只有 union_id。
 */
function findOurBot(mention: LarkMention, bots: LarkBotLookup): LarkBotIdentity | null {
    if (mention.mentioned_type !== 'bot') return null;
    if (mention.bot_info?.app_id) return bots.byAppId(mention.bot_info.app_id);
    if (mention.id.union_id) return bots.byUnionId(mention.id.union_id);
    return null;
}

/**
 * 展示名的取名顺序：人设名 → 飞书给的名字 → 各种 id → 占位符本身。
 *
 * 一路退到 id 是刻意的：空名字会渲染成一个光秃秃的 "@"，读的人完全不知道被 @
 * 的是谁，等于静默丢信息。
 */
function displayNameOf(mention: LarkMention, ourBot: LarkBotIdentity | null): string {
    return (
        ourBot?.displayName ||
        mention.name?.trim() ||
        mention.id.union_id ||
        mention.id.user_id ||
        mention.id.open_id ||
        mention.key
    );
}

/**
 * 把飞书给的 mention 记录逐条解释成"谁、叫什么"。
 *
 * 全部解释、而不是只解释正文里真的出现的那几个：`mentions` 是这条消息 @ 了谁的
 * 完整事实，寻址判定（这条冲不冲 bot 来）看的是这份完整列表，不是正文里的占位符。
 */
export function resolveLarkMentions(
    mentions: readonly LarkMention[],
    bots: LarkBotLookup,
): LarkMentionIndex {
    const all = mentions.map((mention) => {
        const ourBot = findOurBot(mention, bots);
        // 我们自己的 bot 没有 common_user_id = 身份初始化还没跑完。放过去的话
        // 投影会写出一条认不出说话人的记录，而且只在生产的启动竞态里偶发。
        if (ourBot && !ourBot.commonUserId) {
            throw new Error(
                `lark bot "${ourBot.botName}" was mentioned but has no common_user_id; ` +
                    'bot identity initialization must finish before inbound parsing',
            );
        }
        return {
            token: mention.key,
            unionId: mention.id.union_id,
            displayName: displayNameOf(mention, ourBot),
            botCommonUserId: ourBot?.commonUserId,
        };
    });

    const byToken = new Map(all.map((m) => [m.token, m]));
    return { all, byToken: (token) => byToken.get(token) };
}
