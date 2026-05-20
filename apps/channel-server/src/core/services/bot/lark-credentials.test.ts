import { describe, it, expect } from 'bun:test';
import {
    larkCredentials,
    type LarkCredentials,
    type ChannelCredentialed,
} from './lark-credentials';

// bot_config 凭据多 channel 化后：飞书凭据全部活在 credentials JSONB 里，旧
// 独立列已删。所有原来读 bot.app_id / bot.robot_union_id / bot.app_secret /
// bot.encrypt_key / bot.verification_token 的调用方，统一改走这个 typed view
// 从 credentials 取。getBotAppId/getBotUnionId 内部也走它。
function larkBot(): ChannelCredentialed {
    return {
        channel: 'lark',
        credentials: {
            app_id: 'cli_app_123',
            app_secret: 'sec_456',
            encrypt_key: 'enc_789',
            verification_token: 'vtok_abc',
            robot_union_id: 'on_union_def',
        },
    };
}

describe('lark-credentials: 从 credentials JSONB 取飞书凭据', () => {
    it('typed view 完整取出五个飞书凭据字段', () => {
        const c: LarkCredentials = larkCredentials(larkBot());
        expect(c.app_id).toBe('cli_app_123');
        expect(c.app_secret).toBe('sec_456');
        expect(c.encrypt_key).toBe('enc_789');
        expect(c.verification_token).toBe('vtok_abc');
        expect(c.robot_union_id).toBe('on_union_def');
    });

    it('非 lark channel 的记录取飞书凭据时明确报错（不静默返回 undefined）', () => {
        const qq: ChannelCredentialed = {
            channel: 'qq',
            credentials: { app_id: 'qq_1', app_secret: 'qq_2', bot_secret: 'qq_3' },
        };
        expect(() => larkCredentials(qq)).toThrow(/lark|飞书/i);
    });

    it('lark 记录但 credentials 缺关键字段时明确报错', () => {
        const broken: ChannelCredentialed = {
            channel: 'lark',
            credentials: { app_id: 'only_app_id' },
        };
        expect(() => larkCredentials(broken)).toThrow();
    });
});
