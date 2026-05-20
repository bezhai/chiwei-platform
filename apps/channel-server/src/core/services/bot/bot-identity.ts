// T5-5b 漏修补丁：chat-response-worker 出站写 conversation_messages 时
// assistant 行的 user_id / username 必须走 IdentityResolver，否则 botName 字符串
// （"dev" 之类）直接落库，破坏 spec「conversation_messages 全字段 internal ULID」
// 承诺。本模块把"bot 也是一种 user 身份"这件事收在一处：
//
//   resolveBotIdentity(botName, identityResolver, lookup) →
//     { globalUserId, displayName? }
//
// - globalUserId：走 identity_user 表的全局 internal_user_id（ULID）。
//   首次出现自动 bootstrap（IdentityResolver.resolve 对 (channel='lark',
//   channel_user_id=robot_union_id) 走 ON CONFLICT (channel, channel_user_id)
//   DO NOTHING 后回取，已在 5a 钉死，本模块不扩 IdentityResolver 接口、
//   复用 user kind）。
// - displayName：persona display_name，不是 botName 字符串。冗余落
//   conversation_messages.username（与入站行同一冗余口径）。
//
// 错误语义：bot 找不到 / channel 不是 lark / credentials 缺字段 → 抛错
// （fail-loud），绝不静默退回 botName（那是本次要修的 miss）。
//
// 注入式签名（不绑全局单例）：本模块只依赖 IdentityResolver 契约 + 一个最小
// BotLookup 结构型接口（包含 getBotConfig / getDisplayNameByAppId 两个方法），
// 与 multiBotManager 解耦，单测可纯跑。运行时由 chat-response-worker 注入
// multiBotManager 实例（满足结构型接口）。

import type { IdentityResolver } from '@core/channels/identity-resolver';
import { LARK_CHANNEL, larkCredentials, type ChannelCredentialed } from './lark-credentials';

// resolveBotIdentity 对底层 bot 查询的全部需求。结构型接口，
// 不绑 multiBotManager 单例。
export interface BotLookup {
    getBotConfig(botName: string): ChannelCredentialed | null;
    getDisplayNameByAppId(appId: string): string | null;
}

export interface BotIdentity {
    // 全局 internal_user_id（ULID 形态）。
    globalUserId: string;
    // persona display_name；查不到为 undefined（不退回 botName，不写脏占位）。
    displayName: string | undefined;
}

export async function resolveBotIdentity(
    botName: string,
    resolver: IdentityResolver,
    lookup: BotLookup,
): Promise<BotIdentity> {
    const botConfig = lookup.getBotConfig(botName);
    if (botConfig === null) {
        // fail-loud：本次要修的就是"不静默退回 botName 字符串"。
        throw new Error(
            `resolveBotIdentity: bot not found for botName="${botName}" ` +
                `(MultiBotManager.getBotConfig returned null)`,
        );
    }

    // 当前只支持 lark channel。T6 接 QQ 时这里要扩成 channel 派发，
    // 但本次不动 IdentityResolver 接口，只走"bot 复用 user kind"。
    if (botConfig.channel !== LARK_CHANNEL) {
        throw new Error(
            `resolveBotIdentity: only lark channel supported, ` +
                `got channel="${botConfig.channel}" for botName="${botName}"`,
        );
    }

    const creds = larkCredentials(botConfig);

    // 关键点：IdentityResolver.resolve('user', 'lark', robot_union_id)
    // —— bot 走 identity_user 表，channel='lark' + channel_user_id=robot_union_id
    // 首次出现自动 INSERT，已有则回取（5a ON CONFLICT 收敛语义）。
    // 不扩 IdentityResolver 接口、不新增 bot kind，复用 user 即可。
    const globalUserId = await resolver.resolve('user', LARK_CHANNEL, creds.robot_union_id);

    // displayName：取 persona display_name（已经被 multiBotManager 预加载到
    // appId→displayName 表里）。查不到留 undefined，不退回 botName。
    const displayName = lookup.getDisplayNameByAppId(creds.app_id) ?? undefined;

    return { globalUserId, displayName };
}
