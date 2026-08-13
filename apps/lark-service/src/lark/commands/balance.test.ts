// 「余额」：管理员问一句，赤尾把 302.ai 的账户情况贴成一张卡片。
//
// 两件事必须钉住：
//
//   * **准入是规则谓词，不是 handler 里的一句 if。** 上游把 IsAdmin 写在 rules 里，所以
//     非管理员敲「余额」是**不命中**（继续往后试别的规则、最后落进聊天），而不是"命中了
//     然后被拒绝"。写成 handler 里的判断会让终态从 no_match 变成 responded，同时把一条
//     本该由聊天接住的消息吃掉。
//   * **每日/总量限额为 0 时显示 `-`，不是 `0.000`。** 那一列的语义是"没有上限"。

import { describe, expect, it } from 'bun:test';
import { runRulesWith } from '@inner/shared/rules';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import type { LarkAiKeyUsage } from './ai-provider';
import { balanceCommand } from './balance';

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

const KEYS: LarkAiKeyUsage[] = [
    {
        api_name: 'gemini',
        limit_daily_cost: 1000,
        current_date_cost: 250,
        limit_cost: 0,
        current_cost: 8000,
    },
    {
        api_name: 'gpt',
        limit_daily_cost: 0,
        current_date_cost: 0,
        limit_cost: 500_000,
        current_cost: 1,
    },
];

interface Did {
    cards: { messageId: string; card: object; inThread: boolean }[];
    replies: { messageId: string; text: string; inThread: boolean }[];
}

function json(card: object): Record<string, any> {
    return JSON.parse(JSON.stringify(card));
}

function rig(
    options: {
        text?: string;
        isAdmin?: boolean;
        mentionsBot?: boolean;
        messageType?: string;
        balanceFails?: boolean;
        keysFail?: boolean;
        keys?: LarkAiKeyUsage[];
    } = {},
) {
    const did: Did = { cards: [], replies: [] };
    const mentionsBot = options.mentionsBot ?? true;

    const deps = {
        api: {
            replyCard: async (messageId: string, card: object, inThread: boolean) => {
                did.cards.push({ messageId, card, inThread });
                return {};
            },
            replyText: async (messageId: string, text: string, inThread: boolean) => {
                did.replies.push({ messageId, text, inThread });
                return {};
            },
        },
        aiProvider: {
            balance: async () => {
                if (options.balanceFails) throw new Error('302 is down');
                return '123.45';
            },
            apiKeys: async () => {
                if (options.keysFail) throw new Error('302 is down');
                return options.keys ?? KEYS;
            },
        },
    } as unknown as LarkCommandDeps;

    const text = options.text ?? '余额';
    const messageType = options.messageType ?? 'text';
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
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
        did,
        run: () =>
            runRulesWith(message, {
                chatRules: [balanceCommand(deps)(context)],
                botRole: 'utility',
                notBlocked: async () => true,
            }),
    };
}

describe('余额：卡片', () => {
    it('余额一行 markdown，用量一张五列的表', async () => {
        const { did, run } = rig();

        const terminal = await run();

        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('发送余额信息');
        expect(did.cards).toHaveLength(1);
        expect(did.cards[0]!.messageId).toBe('om_1');
        // 拆分前 replyCard 没传 replyInThread，即不进话题。
        expect(did.cards[0]!.inThread).toBe(false);

        const card = json(did.cards[0]!.card);
        expect(card.header.title.content).toBe('302AI使用情况');
        expect(card.header.template).toBe('orange');

        const markdown = card.body.elements[0];
        expect(markdown.tag).toBe('markdown');
        expect(markdown.content).toBe('**当前余额：** 123.45');

        const table = card.body.elements[1];
        expect(table.tag).toBe('table');
        expect(table.columns.map((c: any) => [c.name, c.display_name])).toEqual([
            ['api_name', 'API名称'],
            ['limit_daily_cost', '每日上限'],
            ['current_date_cost', '今日消耗'],
            ['limit_cost', '消耗总上限'],
            ['current_cost', '当前总消耗'],
        ]);
    });

    // 金额是"千分之一"单位存的，除以 1000 之后保留三位；**0 显示 `-` 而不是 0.000**，
    // 因为那一列的 0 表示"没有上限"，不是"上限是零"。
    it('金额除以 1000 保留三位，0 显示成 -', async () => {
        const { did, run } = rig();

        await run();

        expect(json(did.cards[0]!.card).body.elements[1].rows).toEqual([
            {
                api_name: 'gemini',
                limit_daily_cost: '1.000',
                current_date_cost: '0.250',
                limit_cost: '-',
                current_cost: '8.000',
            },
            {
                api_name: 'gpt',
                limit_daily_cost: '-',
                current_date_cost: '-',
                limit_cost: '500.000',
                current_cost: '0.001',
            },
        ]);
    });

    it('一个 key 都没有时表是空的，卡片照发', async () => {
        const { did, run } = rig({ keys: [] });

        await run();

        expect(json(did.cards[0]!.card).body.elements[1].rows).toEqual([]);
    });
});

describe('余额：查不到就说一句', () => {
    it('余额查询失败：回一句固定的话，不发卡片', async () => {
        const { did, run } = rig({ balanceFails: true });

        const terminal = await run();

        expect(terminal.kind).toBe('responded');
        expect(did.cards).toEqual([]);
        expect(did.replies).toEqual([
            { messageId: 'om_1', text: '获取余额信息失败', inThread: true },
        ]);
    });

    it('用量查询失败也一样', async () => {
        const { did, run } = rig({ keysFail: true });

        await run();

        expect(did.cards).toEqual([]);
        expect(did.replies).toHaveLength(1);
    });
});

describe('余额：准入写在谓词里', () => {
    // 关键差别：非管理员是 **no_match**（这条消息继续往后走、最后由聊天接住），不是
    // "命中之后被拒绝"。把 IsAdmin 挪进 handler 会让终态变成 responded，@ 赤尾说
    // 「余额」的普通人从此得不到任何回应。
    it('非管理员不命中，把机会留给后面的规则', async () => {
        const { did, run } = rig({ isAdmin: false });

        const terminal = await run();

        expect(terminal.kind).toBe('no_match');
        expect(did.cards).toEqual([]);
        expect(did.replies).toEqual([]);
    });

    it('整句相等，不是包含', async () => {
        expect((await rig({ text: '查余额' }).run()).kind).toBe('no_match');
        expect((await rig({ text: '余额多少' }).run()).kind).toBe('no_match');
    });

    it('群里没 @ 到我不命中', async () => {
        expect((await rig({ mentionsBot: false }).run()).kind).toBe('no_match');
    });

    it('非纯文本消息不命中', async () => {
        expect((await rig({ messageType: 'image' }).run()).kind).toBe('no_match');
    });
});
