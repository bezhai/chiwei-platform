// 「指令处理」那一格：谓词、分发、以及 `/blocklist` 一次跑两条那个既有形态。
//
// 这里跑的是**真的规则引擎和真的账本**，所以顺带钉住了"九条子指令都接上了"——
// 少接一条 `larkSlashDispatch` 在装配期就炸（半搬状态），账本和本体没法各说各话。

import { describe, expect, it } from 'bun:test';
import { runRulesWith } from '@inner/shared/rules';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommandContext } from '../rules/command-context';
import { LARK_SLASH_COMMANDS, type LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import { matchesSlashKey, slashCommand } from './slash';

const APP_ID = 'cli_tool';
const BOT_NAME = 'tool';
const BOT_COMMON_USER_ID = 'cu_bot_tool';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: null, commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

function rig(
    options: {
        text?: string;
        mentionsBot?: boolean;
        messageType?: string;
        isAdmin?: boolean;
        blocked?: string[];
        replyFails?: boolean;
    } = {},
) {
    const said: string[] = [];
    const blocked = new Set(options.blocked ?? []);

    const deps = {
        api: {
            replyText: async (_messageId: string, text: string) => {
                if (options.replyFails) throw new Error('lark refused');
                said.push(text);
                return {};
            },
            getUser: async () => null,
        },
        store: {
            larkGroupMember: async () => null,
            larkGroupBinding: async () => null,
            larkMessage: async () => null,
            insertLarkGroupBinding: async () => {},
            setLarkGroupBindingActive: async () => {},
        },
        database: {
            getRepository: () => ({
                findOne: async ({ where }: { where: { union_id?: string } }) =>
                    where.union_id !== undefined && blocked.has(where.union_id)
                        ? { union_id: where.union_id }
                        : null,
                save: async () => {},
                delete: async () => {},
                find: async () => [...blocked].map((union_id) => ({ union_id })),
            }),
        },
    } as unknown as LarkCommandDeps;

    const mentionsBot = options.mentionsBot ?? true;
    const text = options.text ?? '/chat_id';
    const messageType = options.messageType ?? 'text';
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_admin' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: messageType,
            content:
                messageType === 'text'
                    ? JSON.stringify({ text: mentionsBot ? `@_user_1 ${text}` : text })
                    : JSON.stringify({ image_key: 'img_1' }),
            mentions: mentionsBot
                ? [
                      {
                          key: '@_user_1',
                          id: { union_id: 'on_bot_tool' },
                          name: 'tool-raw',
                          mentioned_type: 'bot',
                          bot_info: { app_id: APP_ID },
                      },
                  ]
                : [],
        },
    };

    const reading = readLarkMessageEvent(event, bots);
    if (!reading) throw new Error('fixture is not a message event');

    const recorded: LarkRecordedInbound = {
        projection: {
            commonUserId: 'cu_sender',
            commonConversationId: 'cc_1',
            commonMessageId: 'cm_1',
            commonRootMessageId: 'cm_root',
            commonReplyMessageId: undefined,
            mentionedCommonUserIds: mentionsBot ? [BOT_COMMON_USER_ID] : [],
        },
        commands: {
            appId: APP_ID,
            isAdmin: options.isAdmin ?? true,
            permission: {},
            groupChat: null,
        },
    };

    const context = larkCommandContext(reading, recorded, BOT_NAME);
    const message = larkRuleMessage(reading, recorded.projection, {
        botName: BOT_NAME,
        commonUserId: BOT_COMMON_USER_ID,
    });

    return {
        said,
        run: () =>
            runRulesWith(message, {
                chatRules: [slashCommand(deps)(context)],
                botRole: 'utility',
                notBlocked: async () => true,
            }),
    };
}

describe('前缀匹配', () => {
    it('以 /{key} 开头才算', () => {
        expect(matchesSlashKey('/chat_id', 'chat_id')).toBe(true);
        expect(matchesSlashKey('/chat_id 多余的参数', 'chat_id')).toBe(true);
        expect(matchesSlashKey('查一下 /chat_id', 'chat_id')).toBe(false);
        expect(matchesSlashKey('chat_id', 'chat_id')).toBe(false);
    });

    // 这就是 `/blocklist` 同时命中两条的来源：`^/block` 对 `/blocklist` 成立。
    it('是前缀不是整词', () => {
        expect(matchesSlashKey('/blocklist', 'block')).toBe(true);
        expect(matchesSlashKey('/unblock', 'block')).toBe(false);
    });
});

describe('「指令处理」的谓词', () => {
    it('九个 key 全都能把这条规则叫起来', async () => {
        for (const slot of LARK_SLASH_COMMANDS) {
            const terminal = await rig({ text: `/${slot.key}` }).run();
            expect(terminal.matchedRule).toBe('指令处理');
        }
    });

    // `/config` 拍板删掉（spec 已知缺陷四）。删掉唯一可观测的变化就是它不再被这条规则
    // 接住 —— 落进后面的规则，跟敲一句赤尾不认识的话一样。
    it('/config 不再被接住', async () => {
        expect((await rig({ text: '/config list' }).run()).kind).toBe('no_match');
    });

    it('不以斜杠开头的话不命中', async () => {
        expect((await rig({ text: '帮我查一下 chat_id' }).run()).kind).toBe('no_match');
    });

    it('群里没 @ 到我不命中', async () => {
        expect((await rig({ mentionsBot: false }).run()).kind).toBe('no_match');
    });

    it('非纯文本消息不命中', async () => {
        expect((await rig({ messageType: 'image' }).run()).kind).toBe('no_match');
    });
});

describe('分发', () => {
    it('命中的那条真的跑了', async () => {
        const { said, run } = rig({ text: '/chat_id' });

        await run();

        expect(said).toEqual(['oc_1']);
    });

    // 既有形态：`^/block` 对 `/blocklist` 也成立，而上游那个循环没有 break，于是两条
    // 都跑。管理员敲 `/blocklist` 会先收到一句"请@具体用户进行拉黑"，再收到名单。
    // 照搬 —— 改成"跑第一个"是在改可观测行为。
    it('/blocklist 同时跑 block 和 blocklist，按清单顺序', async () => {
        const { said, run } = rig({ text: '/blocklist', blocked: ['on_a'] });

        await run();

        expect(said).toEqual(['请@具体用户进行拉黑', '黑名单列表:\n1. on_a']);
    });

    it('/unblock 不会连带跑 block', async () => {
        const { said, run } = rig({ text: '/unblock' });

        await run();

        expect(said).toEqual(['请@具体用户进行解除拉黑']);
    });

    // 子指令抛出去的异常不在这一层兜住，整条收敛成 handler_error，与拆分前一致。
    it('子指令抛错时整条收敛成 handler_error', async () => {
        const terminal = await rig({ text: '/chat_id', replyFails: true }).run();

        expect(terminal.kind).toBe('handler_error');
        expect(terminal.detail).toBe('lark refused');
    });
});
