// 飞书指令清单：还没搬过来的那些槽位，以及它们与人格聊天的先后。
//
// 两件事要钉住，而它们失效的时候都不会有任何运行期症状：
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
import type { LarkInboundProjection } from '../projection/inbound-projection';
import {
    DROPPED_SLASH_COMMANDS,
    LARK_COMMANDS,
    LARK_SLASH_COMMANDS,
    larkCommandRules,
    type LarkCommandSlot,
} from './commands';
import { larkChatRules } from './inbound-rules';
import { larkRuleMessage } from './rule-message';

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

const projection: LarkInboundProjection = {
    commonUserId: 'cu_sender',
    commonConversationId: 'cc_1',
    commonMessageId: 'cm_1',
    commonRootMessageId: 'cm_root',
    commonReplyMessageId: undefined,
    mentionedCommonUserIds: [BOT_COMMON_USER_ID],
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
    const reading = readLarkMessageEvent(event, bots);
    if (!reading) throw new Error('test fixture is not a message event');
    return larkRuleMessage(reading, projection, {
        botName: BOT_NAME,
        commonUserId: BOT_COMMON_USER_ID,
    });
}

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

/** 真引擎跑一遍给定的规则序列。lane 上下文照三个入口那样设，人格聊天要读它。 */
function runSequence(chatRules: RuleConfig[], botRole: string | undefined) {
    return context.run(context.createContext('trace-1', { botName: BOT_NAME }), () =>
        runRulesWith(atTheBot(), { chatRules, botRole, notBlocked: async () => true }),
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

    it('斜杠指令组：要迁的加上拍板删掉的，正好是对面那一组子指令', () => {
        expect([...LARK_SLASH_COMMANDS, ...DROPPED_SLASH_COMMANDS].sort()).toEqual(
            [...upstreamSlashKeys()].sort(),
        );
    });

    // 已知缺陷四：/config 写进 lark_base_chat_info.gray_config，而 agent-service 读的是
    // common_conversation.attachment_policy，这条链路本来就是断的。整组要迁、只有它不迁，
    // 所以"对面有、这边没有"必须是被记下来的决定，不是漏掉。
    it('/config 在对面还活着，这边刻意不给它槽位', () => {
        expect(upstreamSlashKeys()).toContain('config');
        expect(LARK_SLASH_COMMANDS).not.toContain('config');
        expect(DROPPED_SLASH_COMMANDS).toContain('config');
    });
});

describe('拼接：填了的槽位按清单顺序进规则序列，没填的不进', () => {
    it('空槽位不产出规则，填好的保持清单里的先后', () => {
        const ran: string[] = [];
        const first = probe('第一条', ran);
        const second = probe('第二条', ran);
        const roster: LarkCommandSlot[] = [
            { name: '还没搬的', pendingIn: 'D4' },
            { name: '第一条', rule: first },
            { name: '也还没搬的', pendingIn: 'D2' },
            { name: '第二条', rule: second },
        ];

        expect(larkCommandRules(roster)).toEqual([first, second]);
    });
});

describe('顺序契约：指令在前，人格聊天在后', () => {
    // bot 没有 bot_role 时引擎不做 category 过滤，序列是纯粹按先后试。人格聊天排到前面
    // 的话，这条 @ 赤尾的消息会先命中它 —— 指令一条都跑不到，而赤尾照常回话。
    it('不过滤 category 时，指令先拿到匹配机会', async () => {
        const ran: string[] = [];
        const sequence = larkChatRules(
            larkCommandRules([{ name: '余额', rule: probe('余额', ran) }]),
        );

        const terminal = await runSequence(sequence, undefined);

        expect(terminal.matchedRule).toBe('余额');
        expect(ran).toEqual(['余额']);
        expect(terminal.pendingChatTrigger).toBeUndefined();
    });

    // 工具 bot 那条路失效得更彻底：引擎撞上 category 对不上且非 fallthrough 的规则时直接
    // return no_match，所以人格聊天只要排在前面，工具 bot 连第一条指令都到不了。
    it('工具 bot 不会先撞上人格聊天而整条序列短路', async () => {
        const ran: string[] = [];
        const sequence = larkChatRules(
            larkCommandRules([{ name: '余额', rule: probe('余额', ran) }]),
        );

        const terminal = await runSequence(sequence, 'utility');

        expect(terminal.kind).toBe('responded');
        expect(ran).toEqual(['余额']);
    });

    // 反过来钉一次：人格聊天确实是排在最后的那条 catch-all，没有指令命中时它接住。
    it('没有指令命中时人格聊天兜底', async () => {
        const terminal = await runSequence(larkChatRules(larkCommandRules([])), undefined);

        expect(terminal.matchedRule).toBe('聊天');
        expect(terminal.pendingChatTrigger).toBeDefined();
    });
});
