// 「开启复读」「关闭复读」。
//
// 两条指令写的是同一处开关，所以它们和复读本身必须是一组：谓词写歪了（少一条
// `OnlyGroup`、把 `NeedRobotMention` 写成 `NeedNotRobotMention`）不会报错，只会让开关
// 在不该被拨动的时候被拨动 —— 而下一个发现的人是"群里怎么开始复读了"。

import { describe, expect, it } from 'bun:test';
import { runRulesWith } from '@inner/shared/rules';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import type { LarkChatPermission } from '../projection/tables';
import { larkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import { closeRepeatCommand, openRepeatCommand } from './toggle';

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
    patches: { chatId: string; patch: Partial<LarkChatPermission> }[];
    replies: { messageId: string; text: string; inThread: boolean }[];
}

function rig(
    options: { text?: string; chatType?: string; mentionsBot?: boolean; storeFails?: boolean } = {},
) {
    const did: Did = { patches: [], replies: [] };
    const mentionsBot = options.mentionsBot ?? true;

    const deps = {
        api: {
            replyText: async (messageId: string, text: string, inThread: boolean) => {
                did.replies.push({ messageId, text, inThread });
                return {};
            },
        },
        store: {
            setLarkChatPermission: async (
                chatId: string,
                patch: Partial<LarkChatPermission>,
            ) => {
                if (options.storeFails) throw new Error('pg is down');
                did.patches.push({ chatId, patch });
            },
        },
    } as unknown as LarkCommandDeps;

    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: options.chatType ?? 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: JSON.stringify({
                text: mentionsBot
                    ? `@_user_1 ${options.text ?? '开启复读'}`
                    : (options.text ?? '开启复读'),
            }),
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

    function run(command: (deps: LarkCommandDeps) => ReturnType<typeof openRepeatCommand>) {
        return runRulesWith(message, {
            chatRules: [command(deps)(context)],
            botRole: 'utility',
            notBlocked: async () => true,
        });
    }

    return { did, run };
}

describe('开启复读', () => {
    it('把 open_repeat_message 合并进这个会话的开关，然后回一句话', async () => {
        const { did, run } = rig({ text: '开启复读' });

        const terminal = await run(openRepeatCommand);

        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('开启复读');
        expect(did.patches).toEqual([{ chatId: 'oc_1', patch: { open_repeat_message: true } }]);
        // 文案是线上历史，逐字照搬。
        expect(did.replies).toEqual([
            {
                messageId: 'om_1',
                text: '呜哇~复读功能已经开启啦！如果在群聊里看到同样的文字或表情连续出现三次的话，人家也会跟着一起复读呢！(。>︿<)_θ',
                inThread: false,
            },
        ]);
    });

    it('别的话不命中', async () => {
        const { run } = rig({ text: '开启复读功能' });
        expect((await run(openRepeatCommand)).kind).toBe('no_match');
    });

    // 没 @ bot 的群消息归复读那条规则（或者压根没人管），不该被这条指令捡走。
    it('没 @ bot 不命中', async () => {
        const { run } = rig({ text: '开启复读', mentionsBot: false });
        expect((await run(openRepeatCommand)).kind).toBe('no_match');
    });

    // 私聊里没有"群开关"这回事。少了 OnlyGroup，私聊敲一句就会往
    // lark_base_chat_info 里写一个永远没人读的开关。
    it('私聊不命中', async () => {
        const { run } = rig({ text: '开启复读', chatType: 'p2p' });
        expect((await run(openRepeatCommand)).kind).toBe('no_match');
    });

    // 写库失败就不要说"已经开启啦" —— 用户会以为开好了，下次发现没复读也不知道为什么。
    it('写库失败时不回那句"已经开启啦"', async () => {
        const { did, run } = rig({ text: '开启复读', storeFails: true });

        const terminal = await run(openRepeatCommand);

        expect(terminal.kind).toBe('handler_error');
        expect(did.replies).toEqual([]);
    });
});

describe('关闭复读', () => {
    it('把 open_repeat_message 关掉，然后回一句话', async () => {
        const { did, run } = rig({ text: '关闭复读' });

        const terminal = await run(closeRepeatCommand);

        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('关闭复读');
        expect(did.patches).toEqual([{ chatId: 'oc_1', patch: { open_repeat_message: false } }]);
        expect(did.replies).toEqual([
            {
                messageId: 'om_1',
                text: '诶嘿~复读功能已经关闭啦！人家暂时就不会复读了呢 (｡•́︿•̀｡)',
                inThread: false,
            },
        ]);
    });

    it('「开启复读」不会命中关闭那条', async () => {
        const { run } = rig({ text: '开启复读' });
        expect((await run(closeRepeatCommand)).kind).toBe('no_match');
    });
});
