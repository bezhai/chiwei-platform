// 组装根那几行接线本身。
//
// 为什么单独有这一层：规则段的逻辑由 inbound-rules.test.ts 用手工拼的依赖验，那份
// 测试**永远发现不了"接线接错了"** —— 它自己就是接线人。而这里的错法是静默的：指令
// 清单漏接、bot 角色接错，症状都是"某些消息不响应"，日志干净。
//
// 所以这里跑的是**真的装配出来的那份依赖** + 真的 applyLarkRules，只有 bot 目录是替身。

import { describe, expect, it } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';
import { context } from '@inner/shared/middleware';

import type { LarkEvent } from '../ingress/lark-event';
import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommands, type LarkCommandDeps, type LarkCommandSlot } from './commands';
import { applyLarkRules, assembleLarkRules, type LarkRulesInfra } from './inbound-rules';

const APP_ID = 'cli_chiwei';
const BOT_NAME = 'chiwei';
const BOT_COMMON_USER_ID = 'cu_bot_chiwei';
const PERSONA_ID = 'p_chiwei';

function botConfig(overrides: Partial<BotConfig> = {}): BotConfig {
    return {
        bot_name: BOT_NAME,
        channel: 'lark',
        bot_role: 'persona',
        common_user_id: BOT_COMMON_USER_ID,
        persona_id: PERSONA_ID,
        credentials: {
            app_id: APP_ID,
            app_secret: 's',
            encrypt_key: 'e',
            verification_token: 'v',
            robot_union_id: 'on_bot_chiwei',
        },
        ...overrides,
    } as BotConfig;
}

/** bot 目录的替身。三个方法都是 botDirectory 真身上的那三个。 */
function directory(bots: BotConfig[]) {
    return {
        getAllBotConfigs: () => bots,
        getBotConfig: (botName: string) => bots.find((b) => b.bot_name === botName) ?? null,
        getBotCommonUserId: (botName: string) => {
            const id = bots.find((b) => b.bot_name === botName)?.common_user_id;
            if (!id) throw new Error(`bot ${botName} has no common_user_id`);
            return id;
        },
    };
}

function infrastructure(bots: BotConfig[] = [botConfig()]) {
    const infra: LarkRulesInfra = {
        // 今天一个槽位都没填，所以序列是空的。
        commands: [],
        bots: directory(bots),
        notBlocked: async () => true,
    };
    return { infra };
}

const lookup: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

/** 群里 @ 了赤尾。 */
function atTheBot(text = '在吗'): LarkMessageReading {
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: JSON.stringify({ text: `@_user_1 ${text}` }),
            mentions: [
                {
                    key: '@_user_1',
                    id: { union_id: 'on_bot_chiwei' },
                    name: 'chiwei-raw',
                    mentioned_type: 'bot',
                    bot_info: { app_id: APP_ID },
                },
            ],
        },
    };
    return readLarkMessageEvent(event, lookup)!;
}

const recorded: LarkRecordedInbound = {
    projection: {
        commonUserId: 'cu_sender',
        commonConversationId: 'cc_1',
        commonMessageId: 'cm_1',
        commonRootMessageId: 'cm_root',
        commonReplyMessageId: undefined,
        mentionedCommonUserIds: [BOT_COMMON_USER_ID],
    },
    commands: { appId: APP_ID, isAdmin: false, permission: {}, groupChat: null },
};

const event: LarkEvent = {
    type: 'im.message.receive_v1',
    payload: {},
    botName: BOT_NAME,
    receivedAt: new Date('2026-09-04T06:50:54.000Z'),
    traceId: 'trace-1',
};

/** 真实装配 + 真实 applyLarkRules，只有 bot 目录是替身。泳道上下文照三个入口那样设。 */
function runAssembled(
    wired: ReturnType<typeof infrastructure>,
    reading: LarkMessageReading = atTheBot(),
    lane?: string,
) {
    const deps = assembleLarkRules(wired.infra);
    return context.run(context.createContext('trace-1', { botName: BOT_NAME, lane }), () =>
        applyLarkRules(deps, reading, recorded, event),
    );
}

describe('装配出来的依赖里没有任何出队口', () => {
    // 结构判据。装配根拿不到 broker、拿不到锁、拿不到认领口，所以这一段**没法**发 MQ
    // —— 不是"我们记得别发"。多出任何一个键都要重新解释它凭什么在这里。
    it('装配产出只有规则序列与三件规则装配', () => {
        const { infra } = infrastructure();

        expect(Object.keys(assembleLarkRules(infra)).sort()).toEqual([
            'botCommonUserId',
            'botRoleOf',
            'chatRules',
            'notBlocked',
        ]);
    });

    // 装配期也不该再有任何副作用（拆掉之前它在这里注册 chat.request 富化）。
    it('装配是纯函数：同一份 infra 装两次得到等价依赖', () => {
        const { infra } = infrastructure();

        expect(Object.keys(assembleLarkRules(infra)).sort()).toEqual(
            Object.keys(assembleLarkRules(infra)).sort(),
        );
    });
});

describe('装配出来的几根线', () => {
    it('bot 的角色取自 bot 目录：工具 bot 不认 persona 规则', async () => {
        const wired = infrastructure([botConfig({ bot_role: 'utility' })]);
        wired.infra.commands = larkCommands({} as unknown as LarkCommandDeps, [
            {
                name: '人设的',
                command: () => () => ({
                    rules: [],
                    comment: '人设的',
                    category: 'persona',
                    handler: async () => {},
                }),
            },
        ]);

        const terminal = await runAssembled(wired);

        expect(terminal.kind).toBe('no_match');
    });

    it('黑名单从装配进来', async () => {
        const wired = infrastructure();
        wired.infra.notBlocked = async () => false;

        const terminal = await runAssembled(wired);

        expect(terminal.kind).toBe('blocked');
    });
});

describe('装配出来的规则序列', () => {
    // 装配根把 larkCommands(deps) 的产出递进来。漏了这根线的症状是指令一条都不响应，
    // 而日志干净。
    it('指令清单从装配进来并真的跑起来', async () => {
        const ran: string[] = [];
        const roster: LarkCommandSlot[] = [
            {
                name: '余额',
                // 不声明 category —— 与 channel-server 的「撤回」同形，所以引擎不会因为
                // 这个 bot 是 persona 就把它跳过（那条 botRole 过滤是共享引擎的既有语义）。
                command: () => () => ({
                    rules: [],
                    comment: '余额',
                    handler: async () => {
                        ran.push('余额');
                    },
                }),
            },
        ];
        const wired = infrastructure();
        wired.infra.commands = larkCommands({} as unknown as LarkCommandDeps, roster);

        const terminal = await runAssembled(wired);

        expect(terminal.matchedRule).toBe('余额');
        expect(ran).toEqual(['余额']);
    });

    // 一个指令都没填时序列是空的：一条 @ 赤尾的消息走完谁也不接，收敛成 no_match。
    // 拆掉之前这里必然是 responded / matched="聊天"（那条 catch-all 接住了它）。
    it('没有指令时，一条 @ 赤尾的消息收敛成 no_match', async () => {
        const wired = infrastructure();

        const terminal = await runAssembled(wired);

        expect(terminal.kind).toBe('no_match');
        expect(terminal.matchedRule).toBeUndefined();
    });

    // 依赖在装配期绑一次。每条消息重绑一次的话，客户端池、缓存这些东西会跟着消息一起
    // 被重建 —— 每条消息换一次 tenant token。
    it('指令的依赖在装配期绑定，不随消息重绑', async () => {
        const bound: LarkCommandDeps[] = [];
        const deps = { theRealBundle: true } as unknown as LarkCommandDeps;
        const wired = infrastructure();
        wired.infra.commands = larkCommands(deps, [
            {
                name: '记依赖的',
                command: (given) => {
                    bound.push(given);
                    return () => ({ rules: [], comment: '记依赖的', handler: async () => {} });
                },
            },
        ]);

        await runAssembled(wired);
        await runAssembled(wired);

        expect(bound).toEqual([deps]);
    });
});
