// "这个被 @ 的对象是不是我们自己在跑的 bot？" —— 解析层问这个问题时用的实现。
//
// 解析层只认识 mentions.ts 里那个 LarkBotLookup 端口，不认识 bot 目录、也不认识
// credentials 的形状。本文件把两者接起来，是唯一知道"飞书 app_id / robot_union_id
// 藏在 credentials 这团 JSONB 里"的地方。

import type { BotConfig } from '@inner/shared/entities';

import { LARK_CHANNEL } from './channel';
import { larkCredentials } from './credentials';
import type { LarkBotIdentity, LarkBotLookup } from './message/mentions';

/** bot 目录里我们需要的那一件事。 */
export interface LarkBotRoster {
    getAllBotConfigs(): BotConfig[];
}

/** persona_id → 人设展示名。没绑人设或人设查不到时返回 null。 */
export type LarkPersonaName = (personaId: string) => string | null;

function identityOf(bot: BotConfig, personaName: LarkPersonaName): LarkBotIdentity {
    return {
        botName: bot.bot_name,
        displayName: bot.persona_id ? personaName(bot.persona_id) : null,
        commonUserId: bot.common_user_id,
    };
}

export function createLarkBotLookup(
    roster: LarkBotRoster,
    personaName: LarkPersonaName,
): LarkBotLookup {
    // 每次查都重读目录：common_user_id 是启动时回填的，缓存一份快照会把回填之前的
    // 空值一直留着。目录规模是个位数，遍历不值得优化。
    const find = (matches: (credentials: ReturnType<typeof larkCredentials>) => boolean) => {
        for (const bot of roster.getAllBotConfigs()) {
            // 本进程本来只加载飞书 bot。万一混进别的渠道，问它要飞书凭据会抛错 ——
            // 一条来路不明的记录不该让每条入站消息都炸，跳过就好。
            if (bot.channel !== LARK_CHANNEL) continue;
            if (matches(larkCredentials(bot))) return identityOf(bot, personaName);
        }
        return null;
    };

    return {
        byAppId: (appId) => find((credentials) => credentials.app_id === appId),
        byUnionId: (unionId) => find((credentials) => credentials.robot_union_id === unionId),
    };
}

/**
 * 出站消息上署的名。
 *
 * 署的是**人设名**（"赤尾"），不是飞书后台那个应用名（"XX机器人"）—— 后者是运维
 * 视角的标识，出现在聊天记录里很出戏。
 *
 * **查不到返回 undefined 而不是抛**：署名缺失只是消息上少一个名字，抛错会让整条
 * 回复发不出去。跟 larkAppIdOf 的 fail-loud 不同，因为那个猜错会把人记到别的应用
 * 名下、留下一份错的身份，这个不会。
 */
export function larkDisplayNameOf(
    roster: LarkBotRoster,
    personaName: LarkPersonaName,
    botName: string,
): string | undefined {
    for (const bot of roster.getAllBotConfigs()) {
        if (bot.channel !== LARK_CHANNEL) continue;
        if (bot.bot_name !== botName) continue;
        return (bot.persona_id ? personaName(bot.persona_id) : null) ?? undefined;
    }
    return undefined;
}

/**
 * 处理这条事件的 bot 跑在哪个飞书应用下。
 *
 * 用处只有一个：投影按 (app_id, open_id) 记用户身份，而事件里偶尔没带 app_id。这
 * 时只能回答"接住它的是谁"。**认不出就抛**，不给缺省值 —— 猜一个 app_id 会把这个
 * 人记到别的应用名下，同一个人从此在公共层有两份身份。
 */
export function larkAppIdOf(roster: LarkBotRoster, botName: string): string {
    for (const bot of roster.getAllBotConfigs()) {
        if (bot.channel !== LARK_CHANNEL) continue;
        if (bot.bot_name === botName) return larkCredentials(bot).app_id;
    }
    throw new Error(`no lark bot named "${botName}" in this process; cannot resolve its app id`);
}
