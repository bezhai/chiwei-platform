// 组装根那几行接线本身。
//
// 为什么单独有这一层：规则段的逻辑由 inbound-rules.test.ts 用手工拼的依赖验，那份
// 测试**永远发现不了"接线接错了"** —— 它自己就是接线人。而这里最要命的两个错法都是
// 静默的：
//
//   * **富化漏注册**：共享包 buildChatRequestPayload 找不到 lark 的 enricher 会悄悄
//     退回中性默认，persona_ids 恒空。agent-service 那侧群聊 persona_ids 为空就是不
//     回复 —— 赤尾在所有群里全哑，而每一条测试都是绿的。
//   * **lane 没接上**：publish 的 lane 是第 5 个位置参数，漏了它消息就发进 prod 队列。
//     泳道验证会"看起来正常"，实际跑的是线上那份代码。
//
// 所以这里跑的是**真的装配出来的那份依赖** + 真的 applyLarkRules，只有基础设施是替身。

import { beforeEach, describe, expect, it } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';
import { context } from '@inner/shared/middleware';
import { CHAT_REQUEST, type Route } from '@inner/shared/mq';
import { resetChatRequestEnrichers, type ChatRequestPayload } from '@inner/shared/rules';

import type { LarkEvent } from '../ingress/lark-event';
import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { CommonMessageClaim } from '../projection/tables';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommands, type LarkCommandDeps, type LarkCommandSlot } from './commands';
import {
    applyLarkRules,
    assembleLarkRules,
    type ChatTriggerMarkerStore,
    type LarkRequestBroker,
    type LarkRulesInfra,
} from './inbound-rules';

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

interface BrokerCall {
    route: Route;
    body: Record<string, unknown>;
    delayMs: number | undefined;
    headers: Record<string, unknown> | undefined;
    lane: string | undefined;
}

function infrastructure(bots: BotConfig[] = [botConfig()]) {
    const sent: BrokerCall[] = [];
    const claimed: CommonMessageClaim[] = [];
    const markers = new Map<string, string>();
    const markerCalls: string[] = [];

    const broker: LarkRequestBroker = {
        publish: async (route, body, delayMs, headers, lane) => {
            sent.push({ route, body, delayMs, headers, lane });
        },
    };
    const marker: ChatTriggerMarkerStore = {
        acquire: async (key, token, leaseSeconds) => {
            markerCalls.push(`acquire:${key}:${leaseSeconds}`);
            if (markers.has(key)) return false;
            markers.set(key, token);
            return true;
        },
        release: async (key, token) => {
            markerCalls.push(`release:${key}`);
            if (markers.get(key) === token) markers.delete(key);
        },
    };
    const infra: LarkRulesInfra = {
        // 今天一个槽位都没填，所以序列里只有人格聊天 —— 与拆分前一致。
        commands: [],
        bots: directory(bots),
        store: {
            claimCommonMessageForBot: async (claim) => {
                claimed.push(claim);
            },
        },
        marker,
        broker,
        notBlocked: async () => true,
    };
    return { infra, sent, claimed, markers, markerCalls };
}

const lookup: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

/** 群里 @ 了赤尾。 */
function atTheBot(): LarkMessageReading {
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: '{"text":"@_user_1 在吗"}',
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
    traceId: 'trace-1',
};

/** 真实装配 + 真实 applyLarkRules，只有基础设施是替身。泳道上下文照三个入口那样设。 */
function runAssembled(wired: ReturnType<typeof infrastructure>, lane?: string) {
    const deps = assembleLarkRules(wired.infra);
    return context.run(context.createContext('trace-1', { botName: BOT_NAME, lane }), () =>
        applyLarkRules(deps, atTheBot(), recorded, event),
    );
}

beforeEach(() => {
    // 富化注册表是进程级的。不清掉的话，别的用例注册过的 lark 富化会让"漏注册"这条
    // 测试假绿 —— 那正是本文件要抓的东西。
    resetChatRequestEnrichers();
});

describe('装配出来的 chat.request 出口', () => {
    it('发到 chat_request 路由，lane 走 publish 的泳道参数而不是 header', async () => {
        const wired = infrastructure();

        await runAssembled(wired, 'ppe-x');

        expect(wired.sent).toHaveLength(1);
        expect(wired.sent[0]).toMatchObject({
            route: CHAT_REQUEST,
            delayMs: undefined,
            headers: undefined,
            lane: 'ppe-x',
        });
    });

    it('载荷本身带的泳道与投递用的泳道是同一个', async () => {
        const wired = infrastructure();

        await runAssembled(wired, 'ppe-x');

        const payload = wired.sent[0]!.body as unknown as ChatRequestPayload;
        expect(payload.lane).toBe('ppe-x');
        expect(payload.channel).toBe('lark');
        expect(payload.message_id).toBe('cm_1');
        expect(payload.bot_name).toBe(BOT_NAME);
    });

    // 漏注册富化不会报任何错，只会让群聊 persona_ids 恒空 = 赤尾在群里全哑。
    it('富化跟着装配一起注册，persona_ids 真的填上了', async () => {
        const wired = infrastructure();

        await runAssembled(wired);

        const payload = wired.sent[0]!.body as unknown as ChatRequestPayload;
        expect(payload.persona_ids).toEqual([PERSONA_ID]);
    });
});

describe('装配出来的其余几根线', () => {
    it('bot 的角色取自 bot 目录：工具 bot 不走人设主链路', async () => {
        const wired = infrastructure([botConfig({ bot_role: 'utility' })]);

        const terminal = await runAssembled(wired);

        expect(terminal.kind).toBe('no_match');
        expect(wired.sent).toEqual([]);
    });

    it('认领消息落到 store 上，驼峰按列名翻译过去', async () => {
        const wired = infrastructure();

        await runAssembled(wired);

        expect(wired.claimed).toEqual([
            { common_message_id: 'cm_1', bot_name: BOT_NAME, common_user_id: 'cu_sender' },
        ]);
    });

    it('去重标记带租期抢，抢到之后成功路径不还', async () => {
        const wired = infrastructure();

        await runAssembled(wired);

        expect(wired.markerCalls).toEqual(['acquire:make_reply:cm_1:60']);
        expect(wired.markers.size).toBe(1);
    });

    // 失败路径退回资格走的必须是同一份标记存储，否则退了个寂寞。
    it('publish 失败时资格退回同一份标记存储', async () => {
        const wired = infrastructure();
        wired.infra.broker = {
            publish: async () => {
                throw new Error('broker is down');
            },
        };

        await expect(runAssembled(wired)).rejects.toThrow('broker is down');

        expect(wired.markerCalls).toEqual(['acquire:make_reply:cm_1:60', 'release:make_reply:cm_1']);
        expect(wired.markers.size).toBe(0);
    });
});

describe('装配出来的规则序列', () => {
    // 装配根把 larkCommands(deps) 的产出递进来，这里验它真的排在人格聊天前面。漏了这根
    // 线的症状与"顺序排反了"一模一样：赤尾照常回话，指令一条都不响应，日志干净。
    it('指令清单从装配进来，排在人格聊天前面', async () => {
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
        expect(wired.sent).toEqual([]);
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
