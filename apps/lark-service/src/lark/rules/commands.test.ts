// 飞书指令清单：还没搬过来的那些槽位、它们与人格聊天的先后、以及依赖怎么进来。
//
// 三件事要钉住，而它们失效的时候都不会有任何运行期症状：
//
// **顺序。** 人格聊天的规则只有 `NeedRobotMention`，一条 @ 赤尾的消息它必然命中。指令
// 排在它后面就永远轮不到 —— 赤尾照常回话、日志干净、一条错误都没有。工具 bot 那条路
// 更糟：引擎遇到 category 对不上且非 fallthrough 的规则会**直接收敛成 no_match**，人格
// 聊天排在前面等于让工具 bot 一条指令都跑不到。
//
// **完整性。** 槽位现在全是空的，删掉一个和留着它在行为上完全一样 —— 两种情况下今天的
// 规则序列都只有人格聊天这一条。所以参照物只能来自本服务之外：channel-server 那份还活着
// 的指令清单。照 queues.test.ts 的办法，expected 不写在本文件里、从对面的源码里取；两边
// 各写各的 expected 时，"改实现顺手改 expected"会让两边一起变绿。
//
// **账本不许假绿。** 斜杠子指令那一组曾经只是一串字符串，用「迁移清单 ∪ 放弃清单 == 上游
// 全集」加两条 `toContain` 校验 —— 把 `block` 从前者挪到后者，并集不变、`config` 照样在，
// 全绿。现在放弃清单是**精确**断言，迁移清单是真正驱动分发的槽位。
//
// Task F 删掉 channel-server 那份清单的时候，本文件的对账用途也就结束了 —— 那时所有槽位
// 应该都已经填满，跨服务对账连同 pendingIn 那个分支一起删。

import { describe, expect, it } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

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
    type LarkSlashSlot,
} from './commands';
import { larkChatRules } from './inbound-rules';

// ---------------------------------------------------------------------------
// 对面那份清单
// ---------------------------------------------------------------------------

const CHANNEL_SERVER_LARK = resolve(import.meta.dir, '../../../../channel-server/src/plugins/lark');

function readUpstream(relativePath: string): string {
    const path = resolve(CHANNEL_SERVER_LARK, relativePath);
    try {
        return readFileSync(path, 'utf8');
    } catch {
        throw new Error(
            `读不到 ${path}。如果是 Task F 已经把 channel-server 的飞书代码删了，` +
                `那本文件的跨服务对账已经没有参照物 —— 确认所有槽位都填满之后把它删掉。`,
        );
    }
}

function matchAll(source: string, pattern: RegExp): string[] {
    return [...source.matchAll(pattern)].map((m) => m[1]!);
}

/** channel-server 那份顶层指令的名字，按它在文件里的先后。 */
function upstreamCommandNames(): string[] {
    return matchAll(readUpstream('commands.ts'), /comment:\s*'([^']*)'/g);
}

/** channel-server 那个斜杠指令组里的子指令 key。 */
function upstreamSlashKeys(): string[] {
    return matchAll(readUpstream('commands/command-handler.ts'), /key:\s*'([^']*)'/g);
}

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

describe('清单完整性：对账 channel-server 那份还活着的指令', () => {
    // 删掉一个槽位、或者把顺序改了，这条就红。顺序也在断言里：Meme 的谓词只有
    // NeedRobotMention 加一条 async 判定，本身就近似 catch-all，排到 EqualText 那几条
    // 前面会把它们全吃掉。
    it('顶层槽位逐条对上，先后也一样', () => {
        expect(LARK_COMMANDS.map((slot) => slot.name)).toEqual(upstreamCommandNames());
    });

    // 槽位填没填**在行为上看不出来**：空槽位不产出规则，人格聊天照样兜底，于是"把一条
    // 已经搬好的指令悄悄改回 pendingIn"能让整套测试全绿，线上表现只是那条指令不再响应
    // （而且人设 bot 本来就跳过 utility，连"没反应"都不新鲜）。所以填充状态本身要有断言。
    // 每批搬完更新这张表。
    it('账本的填充状态：哪些搬完了、哪些还欠着', () => {
        expect(
            LARK_COMMANDS.map((slot) => [slot.name, 'command' in slot ? 'migrated' : slot.pendingIn]),
        ).toEqual([
            ['复读功能', 'migrated'],
            ['发送余额信息', 'D4'],
            ['给用户发送帮助信息', 'migrated'],
            ['撤回消息', 'D4'],
            ['生成水群历史卡片', 'D4'],
            ['开启复读', 'migrated'],
            ['关闭复读', 'migrated'],
            ['指令处理', 'D4'],
            ['发送图片', 'migrated'],
            ['Meme', 'D4'],
        ]);
    });

    // 上一条只看账本，这一条看真的拼出来了什么 —— 槽位填了个别的东西同样是静默的。
    it('真的拼出来的就是账本上那几条，先后也一样', () => {
        expect(larkCommands(DEPS).map((command) => command(commandContext()).comment)).toEqual([
            '复读功能',
            '给用户发送帮助信息',
            '开启复读',
            '关闭复读',
            '发送图片',
        ]);
    });

    it('斜杠指令组：要迁的加上拍板删掉的，正好是对面那一组子指令', () => {
        expect(
            [...LARK_SLASH_COMMANDS.map((slot) => slot.key), ...DROPPED_SLASH_COMMANDS].sort(),
        ).toEqual([...upstreamSlashKeys()].sort());
    });

    // 已知缺陷四：/config 写进 lark_base_chat_info.gray_config，而 agent-service 读的是
    // common_conversation.attachment_policy，这条链路本来就是断的。整组要迁、只有它不迁，
    // 所以"对面有、这边没有"必须是被记下来的决定，不是漏掉。
    //
    // **精确断言，不是 toContain**：并集那条对账对"把 block 从迁移清单挪进放弃清单"是
    // 瞎的（并集不变），只有钉死"放弃的有且仅有 config"才拦得住。
    it('放弃的子指令有且仅有 /config', () => {
        expect(DROPPED_SLASH_COMMANDS).toEqual(['config']);
        expect(upstreamSlashKeys()).toContain('config');
        expect(LARK_SLASH_COMMANDS.map((slot) => slot.key)).not.toContain('config');
    });

    // 「指令处理」那一格的本体**就是**斜杠分发。两边不同步的后果各有各的静默：顶层填了
    // 而子指令还欠着 → 用户敲 /block 掉进人格聊天（赤尾开始闲聊）；顶层还欠着而子指令
    // 填了 → 本体永远不跑，账本却说搬完了。
    it('斜杠子指令与「指令处理」那一格同进同退', () => {
        const group = LARK_COMMANDS.find((slot) => slot.name === '指令处理');
        const filled = LARK_SLASH_COMMANDS.filter((slot) => 'run' in slot).length;

        expect(group).toBeDefined();
        expect('pendingIn' in group!).toBe(filled === 0);
        expect(filled === 0 || filled === LARK_SLASH_COMMANDS.length).toBe(true);
    });
});

describe('斜杠账本直接驱动分发', () => {
    const slash = (key: string, ran: string[]): LarkSlashSlot => ({
        key,
        run: () => async () => {
            ran.push(key);
        },
    });

    it('全部还欠着时没有分发表 —— 「指令处理」那一格因此也是空的', () => {
        expect(
            larkSlashDispatch(DEPS, [
                { key: 'bind', pendingIn: 'D4' },
                { key: 'block', pendingIn: 'D4' },
            ]),
        ).toBeNull();
    });

    it('全部填好时按 key 编成分发表，本体拿到装配期那份依赖', async () => {
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

        expect(Object.keys(table!)).toEqual(['bind', 'block']);
        expect(seen).toEqual([DEPS]);

        await table!['block']!(null as never, commandContext());
        expect(ran).toEqual(['block']);
    });

    // 半填状态是最坏的一种：没填的那几条既不分发、也不报错，敲 /block 的人会看到赤尾
    // 开始闲聊。清单驱动分发之后它变成装配期一声炸。
    it('填了一半就抛，把还欠着的 key 全写在错误里', () => {
        const ran: string[] = [];

        expect(() =>
            larkSlashDispatch(DEPS, [
                slash('bind', ran),
                { key: 'block', pendingIn: 'D4' },
                { key: 'unblock', pendingIn: 'D4' },
            ]),
        ).toThrow(/block.*unblock|unblock.*block/s);
    });

    it('同一个 key 出现两次就抛（后一个会静默盖掉前一个）', () => {
        const ran: string[] = [];

        expect(() => larkSlashDispatch(DEPS, [slash('bind', ran), slash('bind', ran)])).toThrow(
            /bind/,
        );
    });
});

describe('拼接：填了的槽位按清单顺序进规则序列，没填的不进', () => {
    it('空槽位不产出指令，填好的保持清单里的先后', () => {
        const ran: string[] = [];
        const roster: LarkCommandSlot[] = [
            { name: '还没搬的', pendingIn: 'D4' },
            { name: '第一条', command: () => probeCommand('第一条', ran) },
            { name: '也还没搬的', pendingIn: 'D2' },
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

describe('顺序契约：指令在前，人格聊天在后', () => {
    // bot 没有 bot_role 时引擎不做 category 过滤，序列是纯粹按先后试。人格聊天排到前面
    // 的话，这条 @ 赤尾的消息会先命中它 —— 指令一条都跑不到，而赤尾照常回话。
    it('不过滤 category 时，指令先拿到匹配机会', async () => {
        const ran: string[] = [];
        const sequence = larkChatRules([probeCommand('余额', ran)])(commandContext());

        const terminal = await runSequence(sequence, undefined);

        expect(terminal.matchedRule).toBe('余额');
        expect(ran).toEqual(['余额']);
        expect(terminal.pendingChatTrigger).toBeUndefined();
    });

    // 工具 bot 那条路失效得更彻底：引擎撞上 category 对不上且非 fallthrough 的规则时直接
    // return no_match，所以人格聊天只要排在前面，工具 bot 连第一条指令都到不了。
    it('工具 bot 不会先撞上人格聊天而整条序列短路', async () => {
        const ran: string[] = [];
        const sequence = larkChatRules([probeCommand('余额', ran)])(commandContext());

        const terminal = await runSequence(sequence, 'utility');

        expect(terminal.kind).toBe('responded');
        expect(ran).toEqual(['余额']);
    });

    // 反过来钉一次：人格聊天确实是排在最后的那条 catch-all，没有指令命中时它接住。
    it('没有指令命中时人格聊天兜底', async () => {
        const terminal = await runSequence(larkChatRules([])(commandContext()), undefined);

        expect(terminal.matchedRule).toBe('聊天');
        expect(terminal.pendingChatTrigger).toBeDefined();
    });
});
