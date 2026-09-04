// 飞书指令清单：清单里有哪几条、它们与人格聊天的先后、以及依赖怎么进来。
//
// 三件事要钉住，而它们失效的时候都不会有任何运行期症状：
//
// **顺序。** 人格聊天的规则只有 `NeedRobotMention`，一条 @ 赤尾的消息它必然命中。指令
// 排在它后面就永远轮不到 —— 赤尾照常回话、日志干净、一条错误都没有。工具 bot 那条路
// 更糟：引擎遇到 category 对不上且非 fallthrough 的规则会**直接收敛成 no_match**，人格
// 聊天排在前面等于让工具 bot 一条指令都跑不到。
//
// **完整性。** 少一条在行为上看不出来 —— 人格聊天照样兜底，赤尾照常回话，日志干净。所以
// 清单的内容本身要有断言：顶层钉的是真装出来的那串 `comment`（槽位填了个别的东西同样是
// 静默的），斜杠那组钉的是 key 全集。
//
// **放弃清单不许假绿。** 斜杠子指令那一组曾经用「迁移清单 ∪ 放弃清单 == 全集」加两条
// `toContain` 校验 —— 把 `block` 从前者挪到后者，并集不变、`config` 照样在，全绿。所以
// 两份清单现在都是**精确**断言。

import { describe, expect, it } from 'bun:test';

import { context } from '@inner/shared/middleware';
import { runRulesWith, type RuleConfig } from '@inner/shared/rules';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommandContext, type LarkCommandContext } from './command-context';
import {
    DROPPED_SLASH_COMMANDS,
    LARK_COMMANDS,
    LARK_SLASH_COMMANDS,
    larkCommands,
    larkSlashDispatch,
    type LarkCommand,
    type LarkCommandDeps,
    type LarkCommandSlot,
} from './commands';
import { larkChatRules } from './inbound-rules';

// ---------------------------------------------------------------------------
// 跑规则序列用的固定装置
// ---------------------------------------------------------------------------

const APP_ID = 'cli_chiwei';
const BOT_NAME = 'chiwei';
const BOT_COMMON_USER_ID = 'cu_bot_chiwei';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

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

/** 群里 @ 了赤尾 —— 人格聊天那条 catch-all 一定命中的形状。 */
function atTheBot() {
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: '{"text":"@_user_1 余额"}',
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
    const parsed = readLarkMessageEvent(event, bots);
    if (!parsed) throw new Error('test fixture is not a message event');
    return parsed;
}

function commandContext(): LarkCommandContext {
    return larkCommandContext(atTheBot(), recorded, BOT_NAME);
}

/**
 * 依赖的替身。
 *
 * 本文件一条指令都不真的跑，只验"装配期递进去的就是这一个对象" —— 所以造一个可辨识的
 * 空壳，比手搓十几个方法的假 API / 假仓储诚实得多（真跑指令的测试属于 D2-D4）。
 */
const DEPS = { theRealBundle: true } as unknown as LarkCommandDeps;

/** 一条一定命中的假指令（空谓词数组 = every 恒真），命中就留个脚印。 */
function probe(name: string, ran: string[]): RuleConfig {
    return {
        rules: [],
        handler: async () => {
            ran.push(name);
        },
        comment: name,
        category: 'utility',
    };
}

/** 把 probe 包成一条指令：装配期收依赖，逐消息收上下文。 */
function probeCommand(name: string, ran: string[]): LarkCommand {
    return () => probe(name, ran);
}

/** 真引擎跑一遍给定的规则序列。lane 上下文照三个入口那样设，人格聊天要读它。 */
function runSequence(chatRules: RuleConfig[], botRole: string | undefined) {
    const message = atTheBot();
    return context.run(context.createContext('trace-1', { botName: BOT_NAME }), () =>
        runRulesWith(
            {
                channel: 'lark',
                botName: BOT_NAME,
                commonUserId: recorded.projection.commonUserId,
                commonConversationId: recorded.projection.commonConversationId,
                commonMessageId: recorded.projection.commonMessageId,
                commonRootMessageId: recorded.projection.commonRootMessageId,
                isDirect: false,
                botCommonUserId: BOT_COMMON_USER_ID,
                mentionedUserIds: [BOT_COMMON_USER_ID],
                createTime: Number(message.message.createTime),
                clearText: () => '余额',
                text: () => '余额',
                withoutEmojiText: () => '余额',
                isTextOnly: () => true,
                isStickerOnly: () => false,
                stickerKey: () => '',
                imageKeys: () => [],
            },
            { chatRules, botRole, notBlocked: async () => true },
        ),
    );
}

// ---------------------------------------------------------------------------

describe('清单完整性', () => {
    // 删掉一格、或者把顺序改了，这条就红。顺序也在断言里：Meme 的谓词只有
    // NeedRobotMention 加一条 async 判定，本身就近似 catch-all，排到 EqualText 那几条
    // 前面会把它们全吃掉。
    //
    // 断言的是**真的装出来的**那串 comment，不是清单上的 name —— 槽位填了个别的东西同样
    // 是静默的（人格聊天照样兜底，线上表现只是那条指令不再响应）。
    it('真的拼出来的就是清单上那十条，先后也一样', () => {
        expect(larkCommands(DEPS).map((command) => command(commandContext()).comment)).toEqual([
            '复读功能',
            '发送余额信息',
            '给用户发送帮助信息',
            '撤回消息',
            '生成水群历史卡片',
            '开启复读',
            '关闭复读',
            '指令处理',
            '发送图片',
            'Meme',
        ]);
    });

    // 斜杠那一组同理：少一条的症状是敲 `/block` 掉进人格聊天、看到赤尾开始闲聊。
    //
    // 已知缺陷四：/config 写进 lark_base_chat_info.gray_config，而 agent-service 读的是
    // common_conversation.attachment_policy，这条链路本来就是断的。整组九条都在、只有它
    // 不在，所以"没有它"必须是被记下来的决定，不是漏掉。
    //
    // **两份都是精确断言**：只钉「并集不变」对"把 block 从这份挪进放弃清单"是瞎的。
    it('斜杠子指令有且仅有这九条，放弃的有且仅有 /config', () => {
        expect(LARK_SLASH_COMMANDS.map((slot) => slot.key)).toEqual([
            'chat_id',
            'message_id',
            'bind',
            'unbind',
            'block',
            'unblock',
            'blocklist',
            'session',
            'union_id',
        ]);
        expect(DROPPED_SLASH_COMMANDS).toEqual(['config']);
    });
});

describe('斜杠清单直接驱动分发', () => {
    const slash = (key: string, ran: string[]) => ({
        key,
        run: () => async () => {
            ran.push(key);
        },
    });

    it('按 key 编成分发表，本体拿到装配期那份依赖', async () => {
        const ran: string[] = [];
        const seen: LarkCommandDeps[] = [];
        const table = larkSlashDispatch(DEPS, [
            {
                key: 'bind',
                run: (deps) => {
                    seen.push(deps);
                    return async () => {
                        ran.push('bind');
                    };
                },
            },
            slash('block', ran),
        ]);

        expect(Object.keys(table)).toEqual(['bind', 'block']);
        expect(seen).toEqual([DEPS]);

        await table['block']!(null as never, commandContext());
        expect(ran).toEqual(['block']);
    });

    it('同一个 key 出现两次就抛（后一个会静默盖掉前一个）', () => {
        const ran: string[] = [];

        expect(() => larkSlashDispatch(DEPS, [slash('bind', ran), slash('bind', ran)])).toThrow(
            /bind/,
        );
    });
});

describe('拼接：槽位按清单顺序进规则序列', () => {
    // 先后即优先级（理由见 commands.ts 的文件头），所以装配这一跳不许重排。
    it('保持清单里的先后', () => {
        const ran: string[] = [];
        const roster: LarkCommandSlot[] = [
            { name: '第一条', command: () => probeCommand('第一条', ran) },
            { name: '第二条', command: () => probeCommand('第二条', ran) },
        ];

        const commands = larkCommands(DEPS, roster);

        expect(commands.map((command) => command(commandContext()).comment)).toEqual([
            '第一条',
            '第二条',
        ]);
    });

    // 这是必改二的整条理由：指令 handler 要飞书客户端和存储这些长命依赖，清单是常量的
    // 时候它们只能来自全局单例。现在依赖从装配期一次性递进来。
    it('依赖在装配期递进去一次，不是每条消息递一次', () => {
        const seen: LarkCommandDeps[] = [];
        const roster: LarkCommandSlot[] = [
            {
                name: '收依赖的',
                command: (deps) => {
                    seen.push(deps);
                    return () => probe('收依赖的', []);
                },
            },
        ];

        const commands = larkCommands(DEPS, roster);
        commands[0]!(commandContext());
        commands[0]!(commandContext());

        expect(seen).toEqual([DEPS]);
    });

    // 上下文逐消息进：同一条指令连着处理两条消息，各拿各的事实。混了的话 D2-D4 会在
    // 别人的会话上判开关、对着别人的 om_id 回复。
    it('上下文逐消息进，两条消息各拿各的', () => {
        const seen: string[] = [];
        const commands = larkCommands(DEPS, [
            {
                name: '记上下文的',
                command: () => (context) => {
                    seen.push(context.botName);
                    return probe('记上下文的', []);
                },
            },
        ]);

        commands[0]!(larkCommandContext(atTheBot(), recorded, 'chiwei'));
        commands[0]!(larkCommandContext(atTheBot(), recorded, 'chiwei-second'));

        expect(seen).toEqual(['chiwei', 'chiwei-second']);
    });
});

describe('序列契约：只有指令，没有兜底', () => {
    // 拆掉之前序列尾巴上还拼着一条只有 NeedRobotMention 的人格聊天 catch-all，
    // 一条 @ 赤尾的消息必然命中它。那条支线整条拆了 —— 她要不要开口由她自己每一缝查
    // common_message 时决定，不再由入站这一段替她决定。
    it('指令按清单顺序拿到匹配机会', async () => {
        const ran: string[] = [];
        const sequence = larkChatRules([probeCommand('余额', ran)])(commandContext());

        const terminal = await runSequence(sequence, undefined);

        expect(terminal.matchedRule).toBe('余额');
        expect(ran).toEqual(['余额']);
    });

    it('工具 bot 照样跑得到指令', async () => {
        const ran: string[] = [];
        const sequence = larkChatRules([probeCommand('余额', ran)])(commandContext());

        const terminal = await runSequence(sequence, 'utility');

        expect(terminal.kind).toBe('responded');
        expect(ran).toEqual(['余额']);
    });

    // 没有指令命中时**没有人接住它**，收敛成 no_match —— 这是正确终态，不是漏了什么。
    it('没有指令命中时收敛成 no_match，没有兜底规则', async () => {
        const terminal = await runSequence(larkChatRules([])(commandContext()), undefined);

        expect(terminal.kind).toBe('no_match');
        expect(terminal.matchedRule).toBeUndefined();
    });
});
