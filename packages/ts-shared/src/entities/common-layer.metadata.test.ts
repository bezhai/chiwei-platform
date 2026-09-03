import { describe, expect, it } from 'bun:test';
import { getMetadataArgsStorage } from 'typeorm';

import {
    BotConfig,
    CommonAgentResponse,
    CommonBotPresence,
    CommonConversation,
    CommonMessage,
    CommonUser,
    UserBlacklist,
} from './index';

// 公共层的表名与列名是三方（本服务、其他渠道服务、agent-service）共写的契约。
// 改一个列名就是改跨服务约定，这里把它钉成回归。

function tableName(target: Function): string | undefined {
    return getMetadataArgsStorage().tables.find((t) => t.target === target)?.name;
}

function columnNames(target: Function): string[] {
    return getMetadataArgsStorage()
        .columns.filter((c) => c.target === target)
        .map((c) => (c.options.name as string | undefined) ?? c.propertyName);
}

function columnOptions(target: Function, name: string) {
    return getMetadataArgsStorage().columns.find(
        (c) =>
            c.target === target &&
            ((c.options.name as string | undefined) ?? c.propertyName) === name,
    )?.options;
}

describe('common layer entity metadata', () => {
    it('registers common layer tables', () => {
        expect(tableName(CommonUser)).toBe('common_user');
        expect(tableName(CommonConversation)).toBe('common_conversation');
        expect(tableName(CommonMessage)).toBe('common_message');
        expect(tableName(CommonAgentResponse)).toBe('common_agent_response');
        // 多个渠道的入站 handler 都往这张表写 bot 在会话里的在场状态，
        // 所以它跟其他 common_* 一样归共享包，不能留在任何单个服务里。
        expect(tableName(CommonBotPresence)).toBe('common_bot_presence');
    });

    it('bot_config carries bot common user identity and a channel column', () => {
        const cols = columnNames(BotConfig);
        expect(tableName(BotConfig)).toBe('bot_config');
        expect(cols).toContain('common_user_id');
        // 按 channel 过滤加载依赖这一列存在
        expect(cols).toContain('channel');
    });

    it('user_blacklist keys on a channel-neutral id', () => {
        expect(tableName(UserBlacklist)).toBe('user_blacklist');
        expect(columnNames(UserBlacklist)).toContain('union_id');
    });

    it('common_message carries only common ids, no channel-native raw ids', () => {
        const commonMessageColumns = columnNames(CommonMessage);

        expect(commonMessageColumns).toContain('common_message_id');
        expect(commonMessageColumns).toContain('common_conversation_id');
        expect(commonMessageColumns).toContain('common_user_id');
        // 渠道裸 id 一律不上浮到公共层
        expect(commonMessageColumns).not.toContain('om_id');
        expect(commonMessageColumns).not.toContain('chat_id');
        expect(commonMessageColumns).not.toContain('open_id');
    });

    it('common_message keeps "who did this message name" as nullable common ids', () => {
        const options = columnOptions(CommonMessage, 'mentioned_common_user_ids');
        expect(options).toBeDefined();

        // 被 @ 的人存公共层 id。渠道 id（union_id / open_id）不许进这一列 ——
        // 那正是上一条用例守着的边界。
        expect(options?.type).toBe('uuid');
        expect(options?.array).toBe(true);

        // NULL 和 [] 不是一回事：NULL = 没人算过（改动前的存量行、QQ 行、
        // 新写入方上线前的飞书行），[] = 算过、确实没人被 @。读的一侧把 NULL
        // 当"不知道"。加上 NOT NULL 或者给个默认值，这两件事就被合并了，
        // 而且合并之后再也分不开 —— 存量行会凭空变成"确认没人被 @"。
        expect(options?.nullable).toBe(true);
        expect(options?.default).toBeUndefined();
    });
});
