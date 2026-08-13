// 「帮助」。
//
// 这条指令整个内容都在飞书后台那张卡片模板里，本地只有一个模板 id —— 所以测试真正
// 能钉住的只有两件事，而两件都是静默失效的：**谓词**（整句相等、纯文本、@ 到我）和
// **那个模板 id 的字面量**（打错一个字符飞书只会拒收，用户看到的是"没反应"）。

import { describe, expect, it } from 'bun:test';
import { runRulesWith } from '@inner/shared/rules';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import { HELP_CARD_TEMPLATE, helpCommand } from './help';

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

interface Did {
    templates: { messageId: string; templateId: string; variables: unknown }[];
}

function rig(
    options: {
        text?: string;
        mentionsBot?: boolean;
        chatType?: string;
        messageType?: string;
        replyFails?: boolean;
    } = {},
) {
    const did: Did = { templates: [] };
    const mentionsBot = options.mentionsBot ?? true;

    const deps = {
        api: {
            replyTemplate: async (
                messageId: string,
                templateId: string,
                variables: unknown,
            ) => {
                if (options.replyFails) throw new Error('lark rejected the card');
                did.templates.push({ messageId, templateId, variables });
                return {};
            },
        },
    } as unknown as LarkCommandDeps;

    const text = options.text ?? '帮助';
    const messageType = options.messageType ?? 'text';
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: options.chatType ?? 'group',
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
        commands: { appId: APP_ID, isAdmin: false, permission: {}, groupChat: null },
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
                chatRules: [helpCommand(deps)(context)],
                botRole: 'utility',
                notBlocked: async () => true,
            }),
    };
}

describe('帮助', () => {
    // 模板 id 是本地唯一的一份"内容"。它错了飞书只会拒收这条 reply，用户看到的是
    // 「敲了帮助没反应」—— 所以字面量本身要被断言，不是"调到了 replyTemplate"。
    it('拿飞书后台那张模板卡片回复触发的这条消息', async () => {
        const { did, run } = rig();

        const terminal = await run();

        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('给用户发送帮助信息');
        expect(did.templates).toEqual([
            { messageId: 'om_1', templateId: 'ctp_AAYrltZoypBP', variables: undefined },
        ]);
        expect(HELP_CARD_TEMPLATE).toBe('ctp_AAYrltZoypBP');
    });

    // EqualText 是整句相等，不是包含。少了它，「怎么用帮助啊」也会弹出帮助卡片。
    it('整句相等，不是包含', async () => {
        expect((await rig({ text: '帮助一下' }).run()).kind).toBe('no_match');
        expect((await rig({ text: '看看帮助' }).run()).kind).toBe('no_match');
    });

    it('群里没 @ 到我不命中', async () => {
        expect((await rig({ mentionsBot: false }).run()).kind).toBe('no_match');
    });

    // 私聊直通（NeedRobotMention 对私聊恒真），拆分前就是这样 —— 没有 OnlyGroup。
    it('私聊直接敲「帮助」也命中', async () => {
        const { did, run } = rig({ chatType: 'p2p', mentionsBot: false });

        expect((await run()).kind).toBe('responded');
        expect(did.templates).toHaveLength(1);
    });

    // TextMessageLimit：一张图片消息即使正文碰巧读成「帮助」也不该命中。
    it('非纯文本消息不命中', async () => {
        expect((await rig({ messageType: 'image' }).run()).kind).toBe('no_match');
    });

    // 拆分前这一句是 fire-and-forget（没有 await），失败只会变成一个没人接的 rejection ——
    // 而本进程的 unhandledRejection 处理器是 process.exit(1)，持着飞书长连的进程会被
    // 一次回复失败带走。这里 await 它，失败收敛成引擎的 handler_error。
    it('回复失败收敛成 handler_error，不外溢成 unhandled rejection', async () => {
        const terminal = await rig({ replyFails: true }).run();

        expect(terminal.kind).toBe('handler_error');
        expect(terminal.detail).toBe('lark rejected the card');
    });
});
