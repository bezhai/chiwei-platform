// 投影完成之后：这条消息要不要让赤尾开口，要的话把请求发给 agent-service。
//
// 本文件最重要的一条在「多个 bot，一条消息」那个 describe：同群多个 bot 会各自完整
// 地处理同一条消息，而 chat.request 只能发一次。**保证它的不是按 om_id 取的那把投影
// 锁**（那把锁只包住投影，规则段根本不在里面），而是 `make_reply:<commonMessageId>`
// 这把独立的去重锁。所以那个 describe 里没有任何 om_id 锁 —— 两条流是真的并发跑完
// 整个规则段的。
//
// 落 pending 行、认领 bot、publish 三者与去重锁的**先后顺序被专门排过**，理由见
// inbound-rules.ts。顺序错了不会有任何测试之外的症状（照样能回话），但会留下孤儿
// pending 行、或者在落库失败时让锁空占 60s，所以这里用一条 trace 钉死它。
//
// 跑起来会看到几行 `Failed to create common_agent_response: ...`：走真规则的用例用的
// 是共享包那个真的 makeTextReply，它的 pending 行落库要连库，而测试进程没有库。这正是
// 该看到的 —— pending 行是观测便利，落不进去也绝不该挡住 publish。

import { describe, expect, it } from 'bun:test';
import { context } from '@inner/shared/middleware';
import {
    registerChatRequestEnricher,
    resetChatRequestEnrichers,
    type ChatRequestPayload,
    type PendingChatTrigger,
    type RuleConfig,
    type RuleHandlerContext,
    type RuleMessage,
} from '@inner/shared/rules';

import type { LarkEvent } from '../ingress/lark-event';
import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkChatRequestEnricher } from './chat-request';
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

/** 群里 @ 了赤尾。 */
function atTheBot(): LarkMessageReading {
    return reading({
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
        traceId: 'trace-1',
    };
}

interface Wired {
    deps: LarkRulesDeps;
    trace: string[];
    published: Array<{ payload: ChatRequestPayload; lane: string | undefined }>;
    claimed: Array<{ commonMessageId: string; botName: string; commonUserId: string }>;
    /**
     * 进程内共享的去重锁：key → 持有者 token。两条流跑同一条消息时抢的就是它。
     * 释放**比对 token**，跟真身那段 Lua 一个道理 —— 不能无条件删掉别人的。
     */
    locks: Map<string, string>;
}

function wire(overrides: Partial<LarkRulesDeps> = {}, shared?: Partial<Wired>): Wired {
    const trace = shared?.trace ?? [];
    const published = shared?.published ?? [];
    const claimed = shared?.claimed ?? [];
    const locks = shared?.locks ?? new Map<string, string>();
    let minted = 0;

    return {
        trace,
        published,
        claimed,
        locks,
        deps: {
            chatRules: larkChatRules([]),
            botRoleOf: () => 'persona',
            botCommonUserId: () => BOT_COMMON_USER_ID,
            notBlocked: async () => true,
            claimChatTrigger: async (key) => {
                trace.push(`lock:${key}`);
                if (locks.has(key)) return null;
                const token = `token_${++minted}`;
                locks.set(key, token);
                return token;
            },
            releaseChatTrigger: async (key, token) => {
                trace.push(`release:${key}`);
                if (locks.get(key) === token) locks.delete(key);
            },
            claimMessageForBot: async (claim) => {
                trace.push(`claim:${claim.botName}`);
                claimed.push(claim);
            },
            publishChatRequest: async (payload, lane) => {
                trace.push('publish');
                published.push({ payload, lane });
            },
            ...overrides,
        },
    };
}

function run(
    wired: Wired,
    r: LarkMessageReading = atTheBot(),
    recorded: LarkRecordedInbound = recordedOf([BOT_COMMON_USER_ID]),
    event: LarkEvent = larkEvent(),
) {
    return context.run(
        context.createContext('trace-1', { botName: event.botName, lane: undefined }),
        () => applyLarkRules(wired.deps, r, recorded, event),
    );
}

/** 一条注入的规则：命中就登记一个可观测的待发意图，savePending 是探针。 */
function spyRule(trace: string[], payload: Partial<ChatRequestPayload> = {}): RuleConfig {
    return {
        rules: [],
        comment: '探针',
        handler: async (_message: RuleMessage, ctx?: RuleHandlerContext) => {
            trace.push('handler');
            const pending: PendingChatTrigger = {
                payload: { session_id: 's_1', ...payload } as ChatRequestPayload,
                lane: 'ppe-x',
                dedupeKey: `make_reply:${COMMON_MESSAGE_ID}`,
                savePending: async () => {
                    trace.push('savePending');
                },
            };
            ctx?.registerPendingChatTrigger(pending);
        },
    };
}

// ---------------------------------------------------------------------------

describe('装配：规则序列从参数进，不从进程级注册表取', () => {
    it('跑的是调用方给的那份规则', async () => {
        const wired = wire();
        wired.deps.chatRules = () => [spyRule(wired.trace)];

        const terminal = await run(wired);

        expect(terminal.matchedRule).toBe('探针');
        expect(wired.trace).toContain('handler');
    });

    it('规则序列为空时谁也不响应，也不发任何东西', async () => {
        const wired = wire({ chatRules: () => [] });

        const terminal = await run(wired);

        expect(terminal.kind).toBe('no_match');
        expect(wired.published).toEqual([]);
    });

    // botRole 原本由 runRules 从 bot 目录装配，现在由本模块接上。工具 bot 遇到
    // persona 规则要跳过 —— 不然一条 @ 工具 bot 的消息会让赤尾开口。
    it('bot 的角色参与过滤：工具 bot 不走人设主链路', async () => {
        const wired = wire({ botRoleOf: () => 'utility' });

        const terminal = await run(wired);

        expect(terminal.kind).toBe('no_match');
        expect(wired.published).toEqual([]);
    });

    it('黑名单挡掉的用户不触发任何请求', async () => {
        const wired = wire({ notBlocked: async () => false });

        const terminal = await run(wired);

        expect(terminal.kind).toBe('blocked');
        expect(wired.published).toEqual([]);
        expect(wired.claimed).toEqual([]);
    });
});

describe('聊天主链路', () => {
    it('群里 @ 了赤尾就发 chat.request', async () => {
        const wired = wire();

        const terminal = await run(wired);

        expect(terminal.kind).toBe('responded');
        expect(wired.published).toHaveLength(1);
        expect(wired.published[0]!.payload).toMatchObject({
            channel: 'lark',
            message_id: COMMON_MESSAGE_ID,
            chat_id: 'cc_1',
            root_id: 'cm_root',
            user_id: 'cu_sender',
            bot_name: BOT_NAME,
            is_p2p: false,
            is_canary: false,
        });
    });

    // 群里没 @ 就不该回话。这条同时钉住"没 @ 也照样什么都不发"——落库已经在投影里
    // 做完了，规则段对这条消息本来就只有"不响应"一个正确答案。
    it('群里没 @ 赤尾就什么都不发', async () => {
        const wired = wire();

        const terminal = await run(wired, reading(), recordedOf([]));

        expect(terminal.kind).toBe('no_match');
        expect(wired.published).toEqual([]);
        expect(wired.claimed).toEqual([]);
        expect(wired.trace).toEqual([]);
    });

    it('私聊不需要 @ 就直通', async () => {
        const wired = wire();

        const terminal = await run(wired, reading({ chat_type: 'p2p' }), recordedOf([]));

        expect(terminal.kind).toBe('responded');
        expect(wired.published[0]!.payload.is_p2p).toBe(true);
    });

    // handler 抛错 = 没成功响应，绝不带着待发意图往下走。
    it('handler 抛错时不发请求', async () => {
        const wired = wire();
        wired.deps.chatRules = () => [
            {
                rules: [],
                comment: '会炸的规则',
                handler: async () => {
                    throw new Error('handler is on fire');
                },
            },
        ];

        const terminal = await run(wired);

        expect(terminal.kind).toBe('handler_error');
        expect(wired.published).toEqual([]);
        expect(wired.trace).toEqual([]);
    });
});

describe('接线顺序：去重锁 → 认领 bot → 落 pending 行 → publish', () => {
    it('四步紧邻，顺序不变', async () => {
        const wired = wire();
        wired.deps.chatRules = () => [spyRule(wired.trace)];

        await run(wired);

        expect(wired.trace).toEqual([
            'handler',
            `lock:make_reply:${COMMON_MESSAGE_ID}`,
            `claim:${BOT_NAME}`,
            'savePending',
            'publish',
        ]);
    });

    // 没抢到锁的 bot 到此为止：**不认领、不落 pending 行**。落了就是一条永不完成的
    // 孤儿行（真正会有回复的是抢到锁的那个 bot 的 session）。
    it('没抢到锁就地停住，不留任何痕迹', async () => {
        const wired = wire();
        wired.deps.chatRules = () => [spyRule(wired.trace)];
        // 别的 bot 先到了。
        wired.deps.claimChatTrigger = async (key) => {
            wired.trace.push(`lock:${key}`);
            return null;
        };

        const terminal = await run(wired);

        // 终态仍是"响应了"——规则确实命中了，只是这一份被别人先发了。
        expect(terminal.kind).toBe('responded');
        expect(wired.trace).toEqual(['handler', `lock:make_reply:${COMMON_MESSAGE_ID}`]);
        expect(wired.claimed).toEqual([]);
        expect(wired.published).toEqual([]);
    });

    it('认领写的是当前 bot 与这条消息的发送者', async () => {
        const wired = wire();

        await run(wired);

        expect(wired.claimed).toEqual([
            {
                commonMessageId: COMMON_MESSAGE_ID,
                botName: BOT_NAME,
                commonUserId: 'cu_sender',
            },
        ]);
    });

    // 去重锁的键是全局 common_message_id 口径（跨渠道唯一），不是飞书 om_id。
    it('去重锁按公共层消息 id 取，不按飞书 om_id', async () => {
        const wired = wire();

        await run(wired);

        expect(wired.trace[0]).toBe(`lock:make_reply:${COMMON_MESSAGE_ID}`);
        expect(wired.trace.join()).not.toContain('om_1');
    });
});

// 这把锁同时背着两个意思：「有人正在处理」和「已经发出去了」。抢到之后半路失败的那条
// 流留下的是前者，而读它的人当成后者 —— 于是重投的那一次会走进"别人已经发过了"分支、
// 正常返回、被 ACK，消息就此消失。所以**失败路径必须把锁还回去**，让重投能真的重来。
// 成功路径不还：那时它表达的确实是"已经发出去了"。
describe('半路失败：把锁还回去，让重投能重来', () => {
    it('publish 失败时还锁，并把错误抛给调用方', async () => {
        const wired = wire({
            publishChatRequest: async () => {
                throw new Error('broker is down');
            },
        });

        await expect(run(wired)).rejects.toThrow('broker is down');
        expect(wired.locks.size).toBe(0);
    });

    it('认领消息失败时同样还锁', async () => {
        const wired = wire({
            claimMessageForBot: async () => {
                throw new Error('common_message vanished');
            },
        });

        await expect(run(wired)).rejects.toThrow('common_message vanished');
        expect(wired.locks.size).toBe(0);
    });

    // 这条是整件事的落点：失败之后重投**必须真的能重来**，而不是撞上自己留下的锁。
    it('失败之后重投能重新抢到锁并走完', async () => {
        const shared: Partial<Wired> = {
            trace: [],
            published: [],
            claimed: [],
            locks: new Map<string, string>(),
        };
        const failing = wire(
            {
                publishChatRequest: async () => {
                    throw new Error('broker is down');
                },
            },
            shared,
        );
        await expect(run(failing)).rejects.toThrow('broker is down');

        // MQ 把同一条消息重投回来（同一个 common_message_id、同一份锁存储）。
        const retry = wire({}, shared);
        const terminal = await run(retry);

        expect(terminal.kind).toBe('responded');
        expect(retry.published).toHaveLength(1);
    });

    // 成功路径不能还锁：还了就等于把去重让了出去，同群另一个 bot 会再发一次。
    it('成功走完之后锁留着，重投不会再发一次', async () => {
        const shared: Partial<Wired> = {
            trace: [],
            published: [],
            claimed: [],
            locks: new Map<string, string>(),
        };
        const first = wire({}, shared);
        await run(first);
        expect(first.trace).not.toContain(`release:make_reply:${COMMON_MESSAGE_ID}`);

        const retry = wire({}, shared);
        await run(retry);

        expect(retry.published).toHaveLength(1);
    });

    // 只删自己那把：锁有租期，这中间它可能已经过期、又被另一个 bot 抢走。无条件删就是
    // 把别人正在用的去重删掉，那个 bot 的消息会被再发一遍。
    it('还锁时比对持有者，绝不删掉别人的那把', async () => {
        const locks = new Map<string, string>();
        const wired = wire(
            {
                publishChatRequest: async () => {
                    throw new Error('broker is down');
                },
            },
            { trace: [], published: [], claimed: [], locks },
        );
        // 抢到之后、失败之前，锁过期并易主。
        wired.deps.claimMessageForBot = async () => {
            locks.set(`make_reply:${COMMON_MESSAGE_ID}`, 'another-bots-token');
        };

        await expect(run(wired)).rejects.toThrow('broker is down');

        expect(locks.get(`make_reply:${COMMON_MESSAGE_ID}`)).toBe('another-bots-token');
    });

    // 还锁本身失败不该把原始错误盖掉 —— 调用方要看到的是"为什么没发出去"。
    it('还锁失败时抛的仍是原始错误', async () => {
        const wired = wire({
            publishChatRequest: async () => {
                throw new Error('broker is down');
            },
            releaseChatTrigger: async () => {
                throw new Error('redis is also down');
            },
        });

        await expect(run(wired)).rejects.toThrow('broker is down');
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
        const wired = wire();
        wired.deps.chatRules = larkChatRules([spyContext(seen)]);

        await run(
            wired,
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
        const shared: Partial<Wired> = {
            trace: [],
            published: [],
            claimed: [],
            locks: new Map<string, string>(),
        };
        const admin = wire({}, shared);
        const stranger = wire({}, shared);
        admin.deps.chatRules = larkChatRules([spyContext(seen)]);
        stranger.deps.chatRules = larkChatRules([spyContext(seen)]);

        await Promise.all([
            run(admin, atTheBot(), recordedOf([BOT_COMMON_USER_ID], { isAdmin: true })),
            run(stranger, atTheBot(), recordedOf([BOT_COMMON_USER_ID], { isAdmin: false })),
        ]);

        expect(seen.map((context) => context.isAdmin).sort()).toEqual([false, true]);
    });
});

describe('多个 bot，一条消息', () => {
    // 本批最重要的一条。
    //
    // 同群的两个 bot 会各自完整地处理同一条消息。这里**没有任何 om_id 锁** —— 两条
    // 流是真的并发跑完整个规则段的，因为规则段本来就不在投影锁里面。唯一拦住第二次
    // publish 的是 `make_reply:<commonMessageId>` 这把独立的去重锁。
    async function twoBotsAtOnce(overrides: Partial<LarkRulesDeps> = {}) {
        const shared: Partial<Wired> = {
            trace: [],
            published: [],
            claimed: [],
            locks: new Map<string, string>(),
        };
        const first = wire(overrides, shared);
        const second = wire(overrides, shared);
        // 两条流拿到的是同一条消息的同一份投影（同一个 common_message_id）。
        const recorded = recordedOf([BOT_COMMON_USER_ID, 'cu_bot_second']);

        await Promise.all([
            run(first, atTheBot(), recorded, larkEvent('chiwei')),
            run(second, atTheBot(), recorded, larkEvent('chiwei-second')),
        ]);
        return first;
    }

    it('只发一次 chat.request', async () => {
        const shared = await twoBotsAtOnce();
        expect(shared.published).toHaveLength(1);
    });

    it('只有抢到锁的那个 bot 认领这条消息', async () => {
        const shared = await twoBotsAtOnce();
        expect(shared.claimed).toHaveLength(1);
    });

    it('两条流都真的跑到了取锁那一步（不是其中一条压根没进来）', async () => {
        const shared = await twoBotsAtOnce();
        const attempts = shared.trace.filter((step) => step.startsWith('lock:'));
        expect(attempts).toHaveLength(2);
        expect(new Set(attempts).size).toBe(1);
    });

    it('落 pending 行的也只有抢到锁的那个', async () => {
        const shared: Partial<Wired> = {
            trace: [],
            published: [],
            claimed: [],
            locks: new Map<string, string>(),
        };
        const first = wire({}, shared);
        const second = wire({}, shared);
        first.deps.chatRules = () => [spyRule(first.trace)];
        second.deps.chatRules = () => [spyRule(second.trace)];
        const recorded = recordedOf([BOT_COMMON_USER_ID]);

        await Promise.all([
            run(first, atTheBot(), recorded, larkEvent('chiwei')),
            run(second, atTheBot(), recorded, larkEvent('chiwei-second')),
        ]);

        expect(first.trace.filter((step) => step === 'savePending')).toHaveLength(1);
    });
});

describe('persona_ids 端到端', () => {
    // 决策二：富化走共享包那个全局注册表，装配期写一次。这里连着规则一起跑，验的是
    // "注册上了、而且 publish 出去的载荷里真的有人设"。
    it('被 @ 的已注册 bot 的人设跟着 chat.request 一起发出去', async () => {
        resetChatRequestEnrichers();
        registerChatRequestEnricher(
            'lark',
            larkChatRequestEnricher((commonUserId) =>
                commonUserId === BOT_COMMON_USER_ID ? 'p_chiwei' : undefined,
            ),
        );
        const wired = wire();

        await run(wired, atTheBot(), recordedOf([BOT_COMMON_USER_ID, 'cu_human']));

        expect(wired.published[0]!.payload.persona_ids).toEqual(['p_chiwei']);
        resetChatRequestEnrichers();
    });

    // 没注册就是空数组 —— 群聊那侧读到空就是不回复，所以这条是"忘了注册"的哨兵。
    it('没注册富化时 persona_ids 是空的', async () => {
        resetChatRequestEnrichers();
        const wired = wire();

        await run(wired, atTheBot(), recordedOf([BOT_COMMON_USER_ID]));

        expect(wired.published[0]!.payload.persona_ids).toEqual([]);
    });
});
