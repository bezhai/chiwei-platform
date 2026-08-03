import { describe, expect, it } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';

import { larkCredentials } from './credentials';

function bot(overrides: Partial<BotConfig> = {}): BotConfig {
    return {
        bot_name: 'chiwei',
        channel: 'lark',
        credentials: {
            app_id: 'cli_1',
            app_secret: 'secret',
            encrypt_key: 'enc',
            verification_token: 'vtok',
            robot_union_id: 'on_bot',
        },
        ...overrides,
    } as BotConfig;
}

describe('larkCredentials', () => {
    it('reads the five fields the Lark SDK needs', () => {
        expect(larkCredentials(bot())).toEqual({
            app_id: 'cli_1',
            app_secret: 'secret',
            encrypt_key: 'enc',
            verification_token: 'vtok',
            robot_union_id: 'on_bot',
        });
    });

    // 这个服务只该拿到飞书的凭据。拿别的渠道的 bot 来问飞书凭据，说明加载范围配错
    // 了 —— 那是拆服务白拆，必须炸。
    it('refuses a bot from another channel', () => {
        expect(() => larkCredentials(bot({ channel: 'qq' }))).toThrow(/qq/);
    });

    it('refuses a bot with no credentials at all', () => {
        expect(() => larkCredentials(bot({ credentials: null }))).toThrow(/credentials/);
    });

    // 少一个字段就意味着 SDK 会拿着 undefined 去建连或验签，症状是"起来了但收不到
    // 消息"。宁可起不来。
    it.each([
        'app_id',
        'app_secret',
        'encrypt_key',
        'verification_token',
        'robot_union_id',
    ])('refuses credentials missing %s', (field) => {
        const credentials = { ...(bot().credentials as Record<string, unknown>) };
        delete credentials[field];
        expect(() => larkCredentials(bot({ credentials }))).toThrow(new RegExp(field));
    });

    it('refuses a field that is present but blank', () => {
        expect(() =>
            larkCredentials(
                bot({ credentials: { ...(bot().credentials as object), app_secret: '' } }),
            ),
        ).toThrow(/app_secret/);
    });
});
