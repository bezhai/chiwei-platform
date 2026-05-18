// 飞书凭据的 typed view。bot_config 多 channel 化后，飞书凭据不再是 BotConfig
// 上的独立列，而是统一存进 credentials JSONB（框架不约束 JSONB 形状，形状是
// 各 channel adapter 自己的事）。本文件把"从 credentials 取飞书五件套并校验"
// 这件飞书专有的事收在一处，所有原来读 bot.app_id / bot.robot_union_id /
// bot.app_secret / bot.encrypt_key / bot.verification_token 的调用方统一改走
// 这里，调用方对 schema 形态零感知。

export const LARK_CHANNEL = 'lark';

// 飞书 channel 的凭据结构。仅飞书 adapter + 飞书相关链路解释这个形状。
export interface LarkCredentials {
    app_id: string;
    app_secret: string;
    encrypt_key: string;
    verification_token: string;
    robot_union_id: string;
}

// 取飞书凭据只需要这两个字段的结构形态，不必依赖被 TypeORM 装饰的 BotConfig
// 实体类（保持本模块与 ORM 解耦、单测可纯跑）。
export interface ChannelCredentialed {
    channel: string;
    credentials?: Record<string, unknown> | null;
}

const REQUIRED_FIELDS: (keyof LarkCredentials)[] = [
    'app_id',
    'app_secret',
    'encrypt_key',
    'verification_token',
    'robot_union_id',
];

// 把一条 bot 记录解释成飞书凭据。channel 必须是 lark；缺字段直接抛错而不是
// 静默返回 undefined —— 凭据缺失静默放过会让飞书鉴权在运行时出诡异错。
export function larkCredentials(bot: ChannelCredentialed): LarkCredentials {
    if (bot.channel !== LARK_CHANNEL) {
        throw new Error(
            `larkCredentials() called on a non-lark bot (channel="${bot.channel}"); ` +
                `lark credentials only exist on channel="${LARK_CHANNEL}" records`,
        );
    }
    const c = bot.credentials;
    if (typeof c !== 'object' || c === null) {
        throw new Error('lark bot has no credentials JSONB payload');
    }
    const out = {} as LarkCredentials;
    for (const f of REQUIRED_FIELDS) {
        const v = (c as Record<string, unknown>)[f];
        if (typeof v !== 'string' || v.length === 0) {
            throw new Error(
                `lark credentials missing required field "${f}" (channel=lark)`,
            );
        }
        out[f] = v;
    }
    return out;
}
