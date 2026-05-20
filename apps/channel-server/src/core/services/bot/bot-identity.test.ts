import { describe, it, expect, mock } from 'bun:test';
import type { IdentityResolver } from '@core/channels/identity-resolver';
import type { ChannelCredentialed } from './lark-credentials';

// T5-5b 漏修补丁：chat-response-worker 出站写 conversation_messages 的 assistant
// 行身份列必须走 IdentityResolver，不能直接落 botName 字符串（"dev" 之类）。
//
// 钉死的契约：resolveBotIdentity(botName) → { globalUserId, displayName? }
//   - globalUserId：bot 自己也是 user，走 identity_user 表，
//     channel='lark' + channel_user_id=robot_union_id → 拿到全局 internal_user_id（ULID）。
//     首次出现自动 bootstrap（IdentityResolver.resolve ON CONFLICT INSERT 语义）。
//   - displayName：bot 的真实显示名（persona display_name），不是 botName 字符串。
//
// 错误语义：bot 找不到 / channel 不是 lark / credentials 缺字段 → 抛错（fail-loud），
// 绝不静默落 botName 字符串到 conversation_messages（那是本次要修的 miss）。

interface BotLookup {
    getBotConfig(botName: string): ChannelCredentialed | null;
    getDisplayNameByAppId(appId: string): string | null;
}

function makeLarkBot(
    overrides: Partial<ChannelCredentialed['credentials']> = {},
): ChannelCredentialed {
    return {
        channel: 'lark',
        credentials: {
            app_id: 'cli_app_xyz',
            app_secret: 'sec_abc',
            encrypt_key: 'enc_def',
            verification_token: 'vtok_ghi',
            robot_union_id: 'on_union_chiwei',
            ...overrides,
        },
    };
}

function makeResolver(overrides: Partial<IdentityResolver> = {}): IdentityResolver {
    return {
        resolve: mock(async (_kind, _channel, _channelId) => 'STUB_ULID_26CHARS_PADDING01'),
        toChannel: mock(async () => {
            throw new Error('not used');
        }),
        ...overrides,
    } as IdentityResolver;
}

// 真实 IdentityResolver 的 ULID 形状钉死（10 时间字符 + 16 随机字符 = 26 字符，
// Crockford base32 alphabet）。这里只断言长度/字符集，不断言具体值——具体值
// 由真实 ULID 生成器决定，测试用注入的 stub。
function isUlidLike(s: string): boolean {
    return /^[0-9A-HJKMNP-TV-Z]{26}$/.test(s);
}

describe('resolveBotIdentity: bot 出站身份走 IdentityResolver', () => {
    it('lark bot 第一次出站：用 robot_union_id 走 resolve("user","lark",...) 拿全局 ULID', async () => {
        const { resolveBotIdentity } = await import('./bot-identity');

        let capturedResolveArgs: [string, string, string] | undefined;
        const resolver = makeResolver({
            resolve: mock(async (kind, channel, channelId) => {
                capturedResolveArgs = [kind, channel, channelId];
                // 模拟真实 ULID 形状（26 chars Crockford base32）
                return '01K0123456789ABCDEFGHJKMNP';
            }),
        });
        const lookup: BotLookup = {
            getBotConfig: (name) => (name === 'chiwei' ? makeLarkBot() : null),
            getDisplayNameByAppId: (appId) => (appId === 'cli_app_xyz' ? '赤尾' : null),
        };

        const out = await resolveBotIdentity('chiwei', resolver, lookup);

        expect(capturedResolveArgs).toEqual(['user', 'lark', 'on_union_chiwei']);
        expect(out.globalUserId).toBe('01K0123456789ABCDEFGHJKMNP');
        expect(isUlidLike(out.globalUserId)).toBe(true);
        // 关键：返回的 user_id 不再是 botName 字符串（"chiwei"/"dev"）
        expect(out.globalUserId).not.toBe('chiwei');
        expect(out.globalUserId).not.toBe('dev');
    });

    it('displayName 来自 persona display_name，不是 botName 字符串', async () => {
        const { resolveBotIdentity } = await import('./bot-identity');

        const resolver = makeResolver();
        const lookup: BotLookup = {
            getBotConfig: () => makeLarkBot(),
            getDisplayNameByAppId: () => '赤尾',
        };

        const out = await resolveBotIdentity('chiwei', resolver, lookup);

        expect(out.displayName).toBe('赤尾');
        expect(out.displayName).not.toBe('chiwei');
    });

    it('没找到 persona display_name 时 displayName 为 undefined（不退回 botName，不落脏占位）', async () => {
        const { resolveBotIdentity } = await import('./bot-identity');

        const resolver = makeResolver();
        const lookup: BotLookup = {
            getBotConfig: () => makeLarkBot(),
            getDisplayNameByAppId: () => null,
        };

        const out = await resolveBotIdentity('chiwei', resolver, lookup);

        expect(out.displayName).toBeUndefined();
    });

    it('bot 找不到 → fail-loud 抛错（不静默退回 botName）', async () => {
        const { resolveBotIdentity } = await import('./bot-identity');

        const resolver = makeResolver();
        const lookup: BotLookup = {
            getBotConfig: () => null,
            getDisplayNameByAppId: () => null,
        };

        await expect(resolveBotIdentity('unknown_bot', resolver, lookup)).rejects.toThrow(
            /unknown_bot|bot.*not.*found|找不到/i,
        );
    });

    it('非 lark channel 的 bot → fail-loud（飞书凭据语义不适用）', async () => {
        const { resolveBotIdentity } = await import('./bot-identity');

        const resolver = makeResolver();
        const lookup: BotLookup = {
            getBotConfig: () => ({
                channel: 'qq',
                credentials: { app_id: 'qq_1', app_secret: 'qq_2', bot_secret: 'qq_3' },
            }),
            getDisplayNameByAppId: () => null,
        };

        await expect(resolveBotIdentity('qq_bot', resolver, lookup)).rejects.toThrow();
    });

    it('IdentityResolver.resolve 是幂等的（bot bootstrap 与后续调用都走同一路径，第二次拿到同一 ULID）', async () => {
        const { resolveBotIdentity } = await import('./bot-identity');

        // 模拟 IdentityResolver ON CONFLICT 收敛：同一 (channel, channelId) 第二次回同一 ULID
        const stored = new Map<string, string>();
        const resolver = makeResolver({
            resolve: mock(async (kind, channel, channelId) => {
                const key = `${kind}|${channel}|${channelId}`;
                const existing = stored.get(key);
                if (existing) return existing;
                const fresh = '01K00000000000000000BOOTSTR';
                stored.set(key, fresh);
                return fresh;
            }),
        });
        const lookup: BotLookup = {
            getBotConfig: () => makeLarkBot(),
            getDisplayNameByAppId: () => '赤尾',
        };

        const first = await resolveBotIdentity('chiwei', resolver, lookup);
        const second = await resolveBotIdentity('chiwei', resolver, lookup);

        expect(first.globalUserId).toBe(second.globalUserId);
        expect(first.globalUserId).toBe('01K00000000000000000BOOTSTR');
    });
});
