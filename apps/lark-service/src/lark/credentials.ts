// bot_config.credentials 是一团不透明的 JSONB —— 共享层刻意不解释它的形状，
// 因为每个渠道要的东西不一样。这里是飞书那一份的唯一解释处。
//
// 全部 fail-loud：少一个字段，飞书 SDK 就会拿着 undefined 去建连或验签，症状是
// "服务起来了、健康检查绿的、就是一条消息都收不到"。宁可起不来。

import type { BotConfig } from '@inner/shared/entities';

import { LARK_CHANNEL } from './channel';

export interface LarkCredentials {
    app_id: string;
    app_secret: string;
    encrypt_key: string;
    verification_token: string;
    robot_union_id: string;
}

const REQUIRED: (keyof LarkCredentials)[] = [
    'app_id',
    'app_secret',
    'encrypt_key',
    'verification_token',
    'robot_union_id',
];

export function larkCredentials(bot: Pick<BotConfig, 'channel' | 'credentials'>): LarkCredentials {
    if (bot.channel !== LARK_CHANNEL) {
        // 本进程只该持有飞书凭据。拿到别的渠道的 bot 说明加载范围配错了。
        throw new Error(
            `larkCredentials() called on a channel="${bot.channel}" bot; ` +
                'this process only owns lark bots',
        );
    }

    const blob = bot.credentials;
    if (typeof blob !== 'object' || blob === null) {
        throw new Error('lark bot has no credentials payload');
    }

    const credentials = {} as LarkCredentials;
    for (const field of REQUIRED) {
        const value = (blob as Record<string, unknown>)[field];
        if (typeof value !== 'string' || value.length === 0) {
            throw new Error(`lark credentials are missing "${field}"`);
        }
        credentials[field] = value;
    }
    return credentials;
}
