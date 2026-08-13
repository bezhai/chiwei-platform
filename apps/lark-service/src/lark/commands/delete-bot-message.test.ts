// 「撤回」：用户回复赤尾说过的某一句、@ 她说「撤回」，她把那一句撤掉。
//
// 三件事失效起来都是安静的，所以逐条钉住：
//
//   * **撤的是被回复的那条，不是触发指令的那条。** 撤错了用户会看到自己的消息消失。
//   * **归属校验拿的是本次事件的 app_id。** 比错了就能撤别人家 bot 的消息（飞书对
//     bot 自己发的消息返回的 sender.id 是 app_id，不是 union_id）。
//   * **这条指令没有 category。** 上游十条里只有它没声明，所以人设 bot 也认它 ——
//     顺手补一个 `category: 'utility'` 会让赤尾从此撤不了自己的消息，而现象只是
//     "敲了撤回没反应"。

import { describe, expect, it } from 'bun:test';
import { runRulesWith } from '@inner/shared/rules';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkMessageInfo } from '../outbound/lark-api';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import { deleteBotMessageCommand } from './delete-bot-message';

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

interface Did {
    looked: string[];
    recalled: string[];
    replies: { messageId: string; text: string; inThread: boolean }[];
}

function messageInfo(over: Partial<LarkMessageInfo> = {}): LarkMessageInfo {
    return {
        messageId: 'om_parent',
        chatId: 'oc_1',
        senderId: APP_ID,
        senderIdType: 'app_id',
        senderType: 'app',
        messageType: 'text',
        mentions: [],
        ...over,
    };
}

function rig(
    options: {
        text?: string;
        parentId?: string;
        messageType?: string;
        mentionsBot?: boolean;
        parent?: LarkMessageInfo | null;
        lookupFails?: string;
        recallFails?: string;
        replyFails?: boolean;
        appId?: string;
    } = {},
) {
    const did: Did = { looked: [], recalled: [], replies: [] };
    const mentionsBot = options.mentionsBot ?? true;

    const deps = {
        api: {
            getMessage: async (messageId: string) => {
                did.looked.push(messageId);
                if (options.lookupFails) throw new Error(options.lookupFails);
                return options.parent === undefined ? messageInfo() : options.parent;
            },
            recall: async (messageId: string) => {
                did.recalled.push(messageId);
                if (options.recallFails) throw new Error(options.recallFails);
            },
            replyText: async (messageId: string, text: string, inThread: boolean) => {
                if (options.replyFails) throw new Error('lark refused the apology too');
                did.replies.push({ messageId, text, inThread });
                return {};
            },
        },
    } as unknown as LarkCommandDeps;

    const text = options.text ?? '撤回';
    const messageType = options.messageType ?? 'text';
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
        message: {
            message_id: 'om_trigger',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: messageType,
            parent_id: 'parentId' in options ? options.parentId : 'om_parent',
            content:
                messageType === 'text'
                    ? JSON.stringify({ text: mentionsBot ? `@_user_1 ${text}` : text })
                    : JSON.stringify({ image_key: 'img_1' }),
            mentions: mentionsBot
                ? [
                      {
                          key: '@_user_1',
                          id: { union_id: 'on_bot_chiwei' },
                          name: 'chiwei-raw',
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
            commonReplyMessageId: 'cm_parent',
            mentionedCommonUserIds: mentionsBot ? [BOT_COMMON_USER_ID] : [],
        },
        commands: {
            appId: options.appId ?? APP_ID,
            isAdmin: false,
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
        run: (botRole: string | undefined = 'persona') =>
            runRulesWith(message, {
                chatRules: [deleteBotMessageCommand(deps)(context)],
                botRole,
                notBlocked: async () => true,
            }),
    };
}

describe('撤回：正常一撤', () => {
    it('撤的是被回复的那条，不是触发指令的那条', async () => {
        const { did, run } = rig();

        const terminal = await run();

        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('撤回消息');
        expect(did.looked).toEqual(['om_parent']);
        expect(did.recalled).toEqual(['om_parent']);
        // 撤成功不说话 —— 消息没了本身就是回执。
        expect(did.replies).toEqual([]);
    });
});

describe('撤回：只撤自己发的', () => {
    // 飞书对 bot 发的消息返回的 sender.id 是 **app_id**，不是 union_id。所以比的是本次
    // 事件的 app_id；比错了就能撤同群别家 bot 的消息。
    it('被回复的那条不是本应用发的就拒绝', async () => {
        const { did, run } = rig({ parent: messageInfo({ senderId: 'cli_someone_else' }) });

        await run();

        expect(did.recalled).toEqual([]);
        expect(did.replies).toEqual([
            {
                messageId: 'om_trigger',
                text: '撤回失败: 只能撤回机器人自己发送的消息',
                inThread: true,
            },
        ]);
    });

    // 同一条消息、换一个应用来处理，判定必须跟着换 —— 用错的 app_id 来源（比如进程
    // 常量而不是这条事件上的）时这条会红。
    it('换一个应用处理同一条消息，归属判定跟着换', async () => {
        // 被回复的那条是 cli_chiwei 发的。本次事件的应用换成别的之后就撤不动了 ——
        // 归属判据要是来自进程常量而不是这条消息的上下文，这里会绿得看不出问题。
        const mine = rig();
        await mine.run();
        expect(mine.did.recalled).toEqual(['om_parent']);

        const theirs = rig({ appId: 'cli_another' });
        await theirs.run();
        expect(theirs.did.recalled).toEqual([]);
        expect(theirs.did.replies[0]!.text).toBe('撤回失败: 只能撤回机器人自己发送的消息');
    });

    it('真人发的消息也撤不了（sender.id 是 union_id，对不上 app_id）', async () => {
        const { did, run } = rig({
            parent: messageInfo({ senderId: 'on_someone', senderIdType: 'union_id' }),
        });

        await run();

        expect(did.recalled).toEqual([]);
    });
});

describe('撤回：说得出为什么失败', () => {
    it('没回复任何消息就敲撤回', async () => {
        const { did, run } = rig({ parentId: undefined });

        await run();

        expect(did.looked).toEqual([]);
        expect(did.recalled).toEqual([]);
        expect(did.replies).toEqual([
            { messageId: 'om_trigger', text: '撤回失败: 没有父消息，无法撤回', inThread: true },
        ]);
    });

    it('被回复的那条查不到（已经不在了）', async () => {
        const { did, run } = rig({ parent: null });

        await run();

        expect(did.recalled).toEqual([]);
        expect(did.replies).toEqual([
            { messageId: 'om_trigger', text: '撤回失败: 父消息为空，无法撤回', inThread: true },
        ]);
    });

    it('查询本身出错也说一句', async () => {
        const { did, run } = rig({ lookupFails: 'lark api 500' });

        await run();

        expect(did.replies).toEqual([
            { messageId: 'om_trigger', text: '撤回失败: lark api 500', inThread: true },
        ]);
    });

    // 超时限、已经被撤过 —— 飞书都返回非 0 code，端口一律抛。
    it('撤回本身被飞书拒绝', async () => {
        const { did, run } = rig({ recallFails: '消息已超过可撤回时间' });

        const terminal = await run();

        expect(terminal.kind).toBe('responded');
        expect(did.replies).toEqual([
            { messageId: 'om_trigger', text: '撤回失败: 消息已超过可撤回时间', inThread: true },
        ]);
    });

    // 连这一句都发不出去时不要往上抛：用户那边已经无从补救，而抛出去只会多一条
    // handler_error。与 ../photo/send-photo.ts 的 apologise 同一个处理。
    it('连那句道歉都失败也不外溢', async () => {
        const { run } = rig({ parent: null, replyFails: true });

        expect((await run()).kind).toBe('responded');
    });
});

describe('撤回：谓词，以及它没有 category', () => {
    // 上游十条指令里只有这一条没声明 category，所以人设 bot 也认它。补一个
    // `category: 'utility'` 会让赤尾从此撤不了自己的消息 —— 而现象只是"没反应"。
    it('人设 bot 也跑得到（其余九条 utility 指令会被跳过）', async () => {
        const { did, run } = rig();

        const terminal = await run('persona');

        expect(terminal.kind).toBe('responded');
        expect(did.recalled).toEqual(['om_parent']);
    });

    it('工具 bot 一样跑得到', async () => {
        const { did, run } = rig();

        await run('utility');

        expect(did.recalled).toEqual(['om_parent']);
    });

    it('整句相等，不是包含', async () => {
        expect((await rig({ text: '撤回一下' }).run()).kind).toBe('no_match');
        expect((await rig({ text: '帮我撤回' }).run()).kind).toBe('no_match');
    });

    it('群里没 @ 到我不命中', async () => {
        expect((await rig({ mentionsBot: false }).run()).kind).toBe('no_match');
    });

    it('非纯文本消息不命中', async () => {
        expect((await rig({ messageType: 'image' }).run()).kind).toBe('no_match');
    });
});
