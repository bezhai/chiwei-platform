// 逐消息的指令上下文：这一条消息的事实，随这一条消息走。
//
// 三条路都试过、都不行，所以才有这个对象（理由写在 command-context.ts 的文件头）：
// 进 RuleMessage 会污染渠道无关契约、各自再查一次库把搭车读的意义抵消掉、进程级上下文
// 本服务已经否决过两次。这里钉的是"该带的都带到了" —— 少带一样，D2-D4 就会掉回那三条
// 路里的某一条，而且是各掉各的。

import { describe, expect, it } from 'bun:test';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommandContext } from './command-context';

const APP_ID = 'cli_chiwei';
const BOT_NAME = 'chiwei';
const BOT_COMMON_USER_ID = 'cu_bot_chiwei';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: (unionId) =>
        unionId === 'on_bot_chiwei'
            ? { botName: BOT_NAME, displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
};

/** 群里 @ 了赤尾又 @ 了一个真人，还是一条回复。 */
function reading(): LarkMessageReading {
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            root_id: 'om_root',
            parent_id: 'om_parent',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: '{"text":"@_user_1 @_user_2 帮我看看"}',
            mentions: [
                {
                    key: '@_user_1',
                    id: { union_id: 'on_bot_chiwei' },
                    name: 'chiwei-raw',
                    mentioned_type: 'bot',
                    bot_info: { app_id: APP_ID },
                },
                {
                    key: '@_user_2',
                    id: { open_id: 'ou_other', union_id: 'on_other' },
                    name: '李四',
                    mentioned_type: 'user',
                },
            ],
        },
    };
    const parsed = readLarkMessageEvent(event, bots);
    if (!parsed) throw new Error('test fixture is not a message event');
    return parsed;
}

const recorded: LarkRecordedInbound = {
    projection: {
        commonUserId: 'cu_sender',
        commonConversationId: 'cc_1',
        commonMessageId: 'cm_1',
        commonRootMessageId: 'cm_root',
        commonReplyMessageId: 'cm_parent',
        mentionedCommonUserIds: [BOT_COMMON_USER_ID, 'cu_other'],
    },
    commands: {
        appId: APP_ID,
        isAdmin: true,
        permission: { open_repeat_message: true },
        groupChat: { name: '水群', user_count: 7, download_has_permission_setting: 'all_members' },
    },
};

describe('larkCommandContext', () => {
    // 飞书裸 id 到 RuleMessage 为止（那是渠道无关契约），但指令是飞书私有的，它们要
    // 拿 om_id 回复、拿 oc_id 发消息、拿 parent_id 找被回复的那条、拿 mention 的
    // union_id 认人。少一样就得再解析一次事件。
    it('飞书原貌原样带过来：om / oc / parent / root / 发送者 / 被 @ 的人', () => {
        const context = larkCommandContext(reading(), recorded, BOT_NAME);

        expect(context.message.messageId).toBe('om_1');
        expect(context.message.chatId).toBe('oc_1');
        expect(context.message.parentId).toBe('om_parent');
        expect(context.message.rootId).toBe('om_root');
        expect(context.message.chatType).toBe('group');
        expect(context.message.sender).toEqual({
            openId: 'ou_user',
            unionId: 'on_user',
            userId: undefined,
        });
        expect(context.message.mentions.map((m) => m.id.union_id)).toEqual([
            'on_bot_chiwei',
            'on_other',
        ]);
    });

    // `/bind` 一族要的是"第一个被 @ 的**真人**"，所以光有裸 mention 不够 —— 还要知道
    // 哪几个是我们自己的 bot。这个判断已经在解析层做过了，不该再做一遍。
    it('哪些被 @ 的人是我们自己的 bot，跟着一起带过来', () => {
        const context = larkCommandContext(reading(), recorded, BOT_NAME);

        expect(context.mentions.byToken('@_user_1')?.botCommonUserId).toBe(BOT_COMMON_USER_ID);
        expect(context.mentions.byToken('@_user_2')?.botCommonUserId).toBeUndefined();
    });

    // 复读要把被 @ 的人重新写成飞书的 `<at user_id=…>` 标签，所以它要的是 @ **还没被
    // 拍平成字**的那份正文 —— RuleMessage 上的 clearText / text 都已经拍平过了。
    it('正文片段带过来，@ 仍是独立的一段', () => {
        const parsed = reading();
        const context = larkCommandContext(parsed, recorded, BOT_NAME);

        // 同一份，不是又解析了一遍：解析层已经把 @ 切出来过了。
        expect(context.content).toBe(parsed.content);
        expect(context.content.some((part) => part.type === 'mention')).toBe(true);
    });

    it('公共层那组 id 带过来（/session 一族要按它查台账）', () => {
        const context = larkCommandContext(reading(), recorded, BOT_NAME);

        expect(context.projection).toEqual(recorded.projection);
    });

    // 这三样是投影顺路读出来的（见 LarkCommandFacts）。到这里为止就是"读了没往下带"，
    // 也正是这次要修的东西。
    it('投影读到的指令事实带过来：is_admin / 开关 / 群资料 / 收到它的飞书应用', () => {
        const context = larkCommandContext(reading(), recorded, BOT_NAME);

        expect(context.isAdmin).toBe(true);
        expect(context.permission).toEqual({ open_repeat_message: true });
        expect(context.groupChat).toEqual({
            name: '水群',
            user_count: 7,
            download_has_permission_setting: 'all_members',
        });
        expect(context.appId).toBe(APP_ID);
    });

    // 同一条群消息会被同群的几个 bot 各处理一遍，每个 bot 得到的是一份 botName 不同的
    // 上下文 —— 「撤回」按它决定这条是不是自己发的。
    it('当前处理这条消息的 bot 由调用方给', () => {
        expect(larkCommandContext(reading(), recorded, 'chiwei-second').botName).toBe(
            'chiwei-second',
        );
    });
});
