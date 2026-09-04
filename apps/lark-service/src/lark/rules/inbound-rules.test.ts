// 投影完成之后：这条消息命中哪条飞书指令。
//
// 「这条消息触发一次聊天请求」这个概念已经不存在了 —— 赤尾不从队列拿消息，她每一缝
// 直接查 common_message、自己决定要不要开口。所以这一段**只剩指令**：跑规则、记一条
// 终态、结束。没有去重锁、没有认领、没有 pending 行、没有 publish。
//
// 落库不在这一段里（投影内部就做完了，见 receive-message.ts），所以这里删掉的东西一
// 件都碰不到它。

import { describe, expect, it } from 'bun:test';
import { context } from '@inner/shared/middleware';
import { EqualText, type RuleConfig, type RuleMessage } from '@inner/shared/rules';

import type { LarkEvent } from '../ingress/lark-event';
import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import type { LarkCommandContext } from './command-context';
import { applyLarkRules, larkChatRules, type LarkRulesDeps } from './inbound-rules';

// ---------------------------------------------------------------------------
// 固定装置
// ---------------------------------------------------------------------------

const APP_ID = 'cli_chiwei';
const BOT_NAME = 'chiwei';
const BOT_COMMON_USER_ID = 'cu_bot_chiwei';
const COMMON_MESSAGE_ID = 'cm_1';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

function reading(overrides: Partial<LarkMessageEvent['message']> = {}): LarkMessageReading {
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: '{"text":"hi"}',
            ...overrides,
        },
    };
    const parsed = readLarkMessageEvent(event, bots);
    if (!parsed) throw new Error('test fixture is not a message event');
    return parsed;
}

/** 群里 @ 了赤尾，正文是一句普通的话（不是任何指令）。 */
function atTheBot(text = '在吗'): LarkMessageReading {
    return reading({
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
    });
}

function recordedOf(
    mentioned: string[] = [],
    commands: Partial<LarkRecordedInbound['commands']> = {},
): LarkRecordedInbound {
    return {
        projection: {
            commonUserId: 'cu_sender',
            commonConversationId: 'cc_1',
            commonMessageId: COMMON_MESSAGE_ID,
            commonRootMessageId: 'cm_root',
            commonReplyMessageId: undefined,
            mentionedCommonUserIds: mentioned,
        },
        commands: {
            appId: APP_ID,
            isAdmin: false,
            permission: {},
            groupChat: null,
            ...commands,
        },
    };
}

function larkEvent(botName = BOT_NAME): LarkEvent {
    return {
        type: 'im.message.receive_v1',
        payload: {},
        botName,
        receivedAt: new Date('2026-09-04T06:50:54.000Z'),
        traceId: 'trace-1',
    };
}

function wire(overrides: Partial<LarkRulesDeps> = {}): LarkRulesDeps {
    return {
        chatRules: larkChatRules([]),
        botRoleOf: () => 'persona',
        botCommonUserId: () => BOT_COMMON_USER_ID,
        notBlocked: async () => true,
        ...overrides,
    };
}

function run(
    deps: LarkRulesDeps,
    r: LarkMessageReading = atTheBot(),
    recorded: LarkRecordedInbound = recordedOf([BOT_COMMON_USER_ID]),
    event: LarkEvent = larkEvent(),
) {
    return context.run(
        context.createContext('trace-1', { botName: event.botName, lane: undefined }),
        () => applyLarkRules(deps, r, recorded, event),
    );
}

/**
 * 一条会留下痕迹的假指令。
 *
 * **默认不声明 category**，与真身里的「撤回」同形：引擎只在规则声明了 category 时才
 * 按 botRole 过滤，所以不声明的指令人设 bot 和工具 bot 都认（见 rule.ts / engine.ts）。
 */
function probe(
    ran: string[],
    comment = '探针',
    rules: RuleConfig['rules'] = [],
    category?: RuleConfig['category'],
): RuleConfig {
    return {
        rules,
        comment,
        ...(category ? { category } : {}),
        handler: async (_message: RuleMessage) => {
            ran.push(comment);
        },
    };
}

// ---------------------------------------------------------------------------

describe('装配：规则序列从参数进，不从进程级注册表取', () => {
    it('跑的是调用方给的那份规则', async () => {
        const ran: string[] = [];
        const terminal = await run(wire({ chatRules: () => [probe(ran)] }));

        expect(terminal.matchedRule).toBe('探针');
        expect(ran).toEqual(['探针']);
    });

    it('规则序列为空时谁也不响应', async () => {
        const terminal = await run(wire({ chatRules: () => [] }));

        expect(terminal.kind).toBe('no_match');
    });

    // botRole 原本由 runRules 从 bot 目录装配，现在由本模块接上。
    it('bot 的角色参与过滤：工具 bot 不认 persona 规则', async () => {
        const ran: string[] = [];
        const terminal = await run(
            wire({
                botRoleOf: () => 'utility',
                chatRules: () => [probe(ran, '人设的', [], 'persona')],
            }),
        );

        expect(terminal.kind).toBe('no_match');
        expect(ran).toEqual([]);
    });

    it('黑名单挡掉的用户，规则一条都不跑', async () => {
        const ran: string[] = [];
        const terminal = await run(
            wire({ notBlocked: async () => false, chatRules: () => [probe(ran)] }),
        );

        expect(terminal.kind).toBe('blocked');
        expect(ran).toEqual([]);
    });
});

describe('飞书指令照常工作', () => {
    // 这一组是整个改动的落点：拆掉聊天主链路之后，指令这条路必须一个字都没变，
    // 包括它对 botRole 的既有语义（下面 describe 专门钉那一条）。
    it('工具 bot 收到 utility 指令照常命中并执行', async () => {
        const ran: string[] = [];
        const terminal = await run(
            wire({
                botRoleOf: () => 'utility',
                chatRules: () => [probe(ran, '余额', [EqualText('余额')], 'utility')],
            }),
            atTheBot('余额'),
        );

        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('余额');
        expect(ran).toEqual(['余额']);
    });

    // 「撤回」是真身十条里唯一没声明 category 的那条，所以人设 bot 也认它。
    it('没声明 category 的指令，人设 bot 也认', async () => {
        const ran: string[] = [];
        const terminal = await run(
            wire({ chatRules: () => [probe(ran, '撤回消息', [EqualText('撤回')])] }),
            atTheBot('撤回'),
        );

        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('撤回消息');
        expect(ran).toEqual(['撤回消息']);
    });

    // 序列里排在前面的指令先拿到匹配机会，谁都没命中才走到 no_match。
    it('没命中的指令逐条留痕，不静默跳过', async () => {
        const ran: string[] = [];
        const terminal = await run(
            wire({
                chatRules: () => [
                    probe(ran, '余额', [EqualText('余额')]),
                    probe(ran, '帮助', [EqualText('帮助')]),
                ],
            }),
            atTheBot('随便说点什么'),
        );

        expect(terminal.kind).toBe('no_match');
        expect(ran).toEqual([]);
        expect(terminal.skipped).toEqual([
            '余额 (rules not satisfied)',
            '帮助 (rules not satisfied)',
        ]);
    });
});

// 这条语义是引擎的，拆分前后一个字没变，但拆掉 catch-all 之后它的**后果**变了，
// 所以在这里钉一次。
//
// 引擎撞上「声明了 category='utility'、而当前 bot 是 persona」的规则时跳过并继续
// （engine.ts 里那个 `botRole === 'persona' && category === 'utility'` 的分支）。
// 真身十条指令里九条声明了 `category: 'utility'`，只有「撤回」没有。所以：
//
//   * 拆分前：赤尾跳过那九条 → 落到序列尾巴的聊天 catch-all → 发 chat.request。
//   * 现在：  赤尾跳过那九条 → 序列走完 → `no_match`。
//
// 也就是说**赤尾从来就不响应那九条指令**，这不是本次改动造成的。变的只是她跳过之后
// 落在哪儿：以前落在聊天上，现在什么都不落。
describe('人设 bot 跳过 utility 指令（引擎既有语义，不是本次改动）', () => {
    it('赤尾对着 utility 指令也走到 no_match，指令 handler 一次都不跑', async () => {
        const ran: string[] = [];
        const terminal = await run(
            wire({
                botRoleOf: () => 'persona',
                chatRules: () => [probe(ran, '余额', [EqualText('余额')], 'utility')],
            }),
            atTheBot('余额'),
        );

        expect(terminal.kind).toBe('no_match');
        expect(ran).toEqual([]);
        expect(terminal.skipped).toEqual(['余额 (botRole=persona != category=utility)']);
    });
});

describe('聊天主链路已经不在这条序列上', () => {
    // 拆掉之前：`larkChatRules` 在指令后面拼一条只有 NeedRobotMention 的 catch-all，
    // 一条 @ 赤尾的消息必然命中它，终态是 responded / matched="聊天"，然后发 MQ。
    //
    // 现在这条 catch-all 没了。一条 @ 赤尾的普通消息走完序列**没有任何规则接住它**，
    // 收敛成 no_match —— 这是正确的终态，不是漏了什么：她要不要回这条消息，由她自己
    // 每一缝查 common_message 时决定，不再由入站这一段替她决定。
    it('群里 @ 了赤尾的普通消息不再命中任何规则', async () => {
        const terminal = await run(wire());

        expect(terminal.kind).toBe('no_match');
        expect(terminal.matchedRule).toBeUndefined();
    });

    it('私聊的普通消息同样不再命中任何规则', async () => {
        const terminal = await run(wire(), reading({ chat_type: 'p2p' }), recordedOf([]));

        expect(terminal.kind).toBe('no_match');
    });

    // 空指令清单拼出来的序列必须是空的。多出任何一条都说明 catch-all 又长回来了。
    it('larkChatRules 不再往序列尾巴上追加任何东西', () => {
        expect(larkChatRules([])({} as LarkCommandContext)).toEqual([]);
    });

    it('larkChatRules 的产出就是指令本身，一条不多', () => {
        const ran: string[] = [];
        const one = probe(ran, '余额');
        const sequence = larkChatRules([() => one])({} as LarkCommandContext);

        expect(sequence).toEqual([one]);
    });
});

describe('规则段的依赖里没有任何出队口', () => {
    // 结构判据，不是纪律判据：这一段拿不到 broker、拿不到锁、拿不到认领口，所以它
    // **没法**发 MQ —— 不是"我们记得别发"。多出任何一个键都要重新解释它凭什么在这里。
    it('依赖只有规则序列与三件规则装配', () => {
        expect(Object.keys(wire()).sort()).toEqual([
            'botCommonUserId',
            'botRoleOf',
            'chatRules',
            'notBlocked',
        ]);
    });
});

describe('指令上下文：拿到的是这一条消息的事实', () => {
    /** 一条把上下文记下来的假指令。 */
    function spyContext(seen: LarkCommandContext[]): (context: LarkCommandContext) => RuleConfig {
        return (context) => {
            seen.push(context);
            return { rules: [], comment: '探针', category: 'utility', handler: async () => {} };
        };
    }

    it('飞书事实与公共层 id 一起进指令，不经由 RuleMessage', async () => {
        const seen: LarkCommandContext[] = [];

        await run(
            wire({ chatRules: larkChatRules([spyContext(seen)]) }),
            atTheBot(),
            recordedOf([BOT_COMMON_USER_ID], {
                isAdmin: true,
                permission: { open_repeat_message: true },
            }),
        );

        expect(seen).toHaveLength(1);
        expect(seen[0]!.isAdmin).toBe(true);
        expect(seen[0]!.permission).toEqual({ open_repeat_message: true });
        expect(seen[0]!.message.messageId).toBe('om_1');
        expect(seen[0]!.projection.commonMessageId).toBe(COMMON_MESSAGE_ID);
        expect(seen[0]!.botName).toBe(BOT_NAME);
    });

    // 这一条是逐消息上下文存在的全部理由。进程级上下文（拆分前那个按 key 存 Message 的
    // 模块级 Map）在这里会挂：两条流并发跑，谁后写谁赢，另一条就在**别人的会话**上判
    // 开关、对着别人的 om_id 回复 —— 而且不报错。
    it('两条消息并发跑时各拿各的事实，不串味', async () => {
        const seen: LarkCommandContext[] = [];
        const admin = wire({ chatRules: larkChatRules([spyContext(seen)]) });
        const stranger = wire({ chatRules: larkChatRules([spyContext(seen)]) });

        await Promise.all([
            run(admin, atTheBot(), recordedOf([BOT_COMMON_USER_ID], { isAdmin: true })),
            run(stranger, atTheBot(), recordedOf([BOT_COMMON_USER_ID], { isAdmin: false })),
        ]);

        expect(seen.map((context) => context.isAdmin).sort()).toEqual([false, true]);
    });
});

describe('同群多个 bot 各自跑自己的规则段', () => {
    // 拆掉之前这里有一把 `make_reply:<commonMessageId>` 去重锁，保证的不变量是
    // 「同群多个 bot，同一条消息只 publish 一次」。不 publish 了之后它什么也不保护：
    // 指令从来就没有被它管过（锁只在拿到待发意图之后才取），两个 bot 各答各的指令
    // 拆分前就是这样。所以锁跟着 publish 一起走。
    it('两个 bot 各自命中自己的指令，互不阻塞', async () => {
        const ran: string[] = [];
        const recorded = recordedOf([BOT_COMMON_USER_ID, 'cu_bot_second']);

        const terminals = await Promise.all([
            run(
                wire({ chatRules: () => [probe(ran, '余额', [EqualText('余额')])] }),
                atTheBot('余额'),
                recorded,
                larkEvent('chiwei'),
            ),
            run(
                wire({ chatRules: () => [probe(ran, '余额', [EqualText('余额')])] }),
                atTheBot('余额'),
                recorded,
                larkEvent('chiwei-second'),
            ),
        ]);

        expect(terminals.map((t) => t.kind)).toEqual(['responded', 'responded']);
        expect(ran).toEqual(['余额', '余额']);
    });
});
