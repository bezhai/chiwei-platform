// 「发图 <标签>」。
//
// 本文件跑的是**真的规则引擎**：谓词接错（少一条 @ 判定、少一条纯文本判定）不会报错，
// 只会让这条指令在不该命中的时候命中 —— 群里有人说一句"发图真好玩"就被塞一张卡片。
//
// 人数闸那张真值表是重点：它决定一个几百人的群能不能刷图。取值错一个方向的后果不对称，
// 所以四种情况各自钉了一条。

import { describe, expect, it } from 'bun:test';
import { runRulesWith, type RuleTerminalState } from '@inner/shared/rules';
import { StatusMode, type ImageForLark } from '@inner/pixiv-client';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import type { LarkChatPermission, LarkGroupChatFacts } from '../projection/tables';
import { larkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import type { LarkReadyPhotos } from './ready';
import { sendPhotoCommand } from './send-photo';

const APP_ID = 'cli_chiwei';
const BOT_NAME = 'tool';
const BOT_COMMON_USER_ID = 'cu_bot_tool';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: '工具', commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

interface Said {
    text: { messageId: string; text: string; inThread: boolean }[];
    cards: { messageId: string; card: object; inThread: boolean }[];
    asked: unknown[];
}

function rig(
    options: {
        chatType?: string;
        text?: string;
        image?: boolean;
        mentionsBot?: boolean;
        permission?: LarkChatPermission;
        groupChat?: LarkGroupChatFacts | null;
        photos?: LarkReadyPhotos;
        replyTextFails?: boolean;
    } = {},
) {
    const said: Said = { text: [], cards: [], asked: [] };
    const mentionsBot = options.mentionsBot ?? true;
    const chatType = options.chatType ?? 'group';

    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: chatType,
            create_time: '1700000000000',
            message_type: options.image ? 'image' : 'text',
            content: options.image
                ? '{"image_key":"img_1"}'
                : JSON.stringify({
                      text: mentionsBot
                          ? `@_user_1 ${options.text ?? '发图 刻晴'}`
                          : (options.text ?? '发图 刻晴'),
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
            commonRootMessageId: 'cm_1',
            commonReplyMessageId: undefined,
            mentionedCommonUserIds: mentionsBot ? [BOT_COMMON_USER_ID] : [],
        },
        commands: {
            appId: APP_ID,
            isAdmin: false,
            permission: options.permission ?? {},
            groupChat: options.groupChat ?? null,
        },
    };

    const deps = {
        api: {
            replyText: async (messageId: string, text: string, inThread: boolean) => {
                said.text.push({ messageId, text, inThread });
                if (options.replyTextFails) throw new Error('lark refused the apology too');
                return {};
            },
            replyCard: async (messageId: string, card: object, inThread: boolean) => {
                said.cards.push({ messageId, card, inThread });
                return {};
            },
        },
        photos:
            options.photos ??
            (async (query) => {
                said.asked.push(query);
                return [
                    { pixiv_addr: 'a.png', image_key: 'a', width: 100, height: 100 },
                    { pixiv_addr: 'b.png', image_key: 'b', width: 100, height: 100 },
                ] satisfies ImageForLark[];
            }),
    } as unknown as LarkCommandDeps;

    const context = larkCommandContext(reading, recorded, BOT_NAME);
    const message = larkRuleMessage(reading, recorded.projection, {
        botName: BOT_NAME,
        commonUserId: BOT_COMMON_USER_ID,
    });

    /** 走真引擎。工具 bot 的 botRole 是 utility，与线上一致。 */
    const runAs = (botRole: string): Promise<RuleTerminalState> =>
        runRulesWith(message, {
            chatRules: [sendPhotoCommand(deps)(context)],
            botRole,
            notBlocked: async () => true,
        });

    return { run: () => runAs('utility'), runAs, said };
}

const A_GROUP = (userCount: number): LarkGroupChatFacts => ({
    name: '群',
    user_count: userCount,
});

// ---------------------------------------------------------------------------

describe('什么样的消息算「发图」', () => {
    it.each([
        ['发图 刻晴', true],
        ['发图', true],
        ['发图刻晴', true],
        ['我要发图', false],
        ['帮助', false],
    ])('%s → 命中=%s', async (text, matched) => {
        const it_ = rig({ text, groupChat: A_GROUP(5) });

        const terminal = await it_.run();

        expect(terminal.matchedRule === '发送图片').toBe(matched);
    });

    // 群里没 @ 就命中的话，任何人说一句"发图……"都会被塞一张卡片。
    it('群聊里没 @ 到自己就不命中', async () => {
        const it_ = rig({ mentionsBot: false, groupChat: A_GROUP(5) });

        expect((await it_.run()).matchedRule).toBeUndefined();
    });

    it('私聊不需要 @', async () => {
        const it_ = rig({ chatType: 'p2p', mentionsBot: false });

        expect((await it_.run()).matchedRule).toBe('发送图片');
    });

    // 图文混排不算指令：带一张图的消息 clearText 里可能也有"发图"两个字。
    it('不是纯文本就不命中', async () => {
        const it_ = rig({ image: true, groupChat: A_GROUP(5) });

        expect((await it_.run()).matchedRule).toBeUndefined();
    });

    // 人设 bot（赤尾）会跳过整类 utility 规则，所以「发图」对她本来就是静默的。
    // 这与拆分前逐字一致，别把它当成接线错误去"修"。
    it('是 utility 类，所以人设 bot 根本跑不到它', async () => {
        const it_ = rig({ groupChat: A_GROUP(5) });

        const asPersona = await it_.runAs('persona');

        expect(asPersona.matchedRule).toBeUndefined();
        expect(it_.said.cards).toEqual([]);
    });
});

describe('标签', () => {
    it('「发图」后面按空白切开就是标签', async () => {
        const it_ = rig({ text: '发图  刻晴   原神 ', groupChat: A_GROUP(5) });

        await it_.run();

        expect(it_.said.asked).toEqual([
            expect.objectContaining({ tag_and_author: ['刻晴', '原神'] }),
        ]);
    });

    // 不带标签就随机发图的话，任何人 @ 一句"发图"都会掉出一张随机卡片。
    it('一个标签都没有时对着用户说一句，不发卡片', async () => {
        const it_ = rig({ text: '发图', groupChat: A_GROUP(5) });

        await it_.run();

        expect(it_.said.cards).toEqual([]);
        expect(it_.said.text).toEqual([
            {
                messageId: 'om_1',
                text: '呜呜~要发图的话，记得带上标签告诉人家想看什么嘛(｡•́︿•̀｡)',
                inThread: true,
            },
        ]);
    });
});

describe('人数闸与两个开关', () => {
    it('私聊永远可以', async () => {
        const it_ = rig({ chatType: 'p2p', mentionsBot: false });

        await it_.run();

        expect(it_.said.cards).toHaveLength(1);
    });

    it.each([
        ['20 人及以下的群直接放行', 20, {}, true],
        ['21 人的群拦住', 21, {}, false],
        ['人多但开了白名单就放行', 500, { allow_send_pixiv_image: true }, true],
    ])('%s', async (_name, userCount, permission, allowed) => {
        const it_ = rig({ groupChat: A_GROUP(userCount), permission });

        await it_.run();

        expect(it_.said.cards).toHaveLength(allowed ? 1 : 0);
        expect(it_.said.text).toHaveLength(allowed ? 0 : 1);
    });

    it('拦住的时候说的是那一句', async () => {
        const it_ = rig({ groupChat: A_GROUP(21) });

        await it_.run();

        expect(it_.said.text[0]!.text).toBe(
            '诶嘿~这个群人有点多呢，发图功能暂时关闭啦(｡•́︿•̀｡) 想用的话可以联系开发者主人帮忙开白哦！',
        );
    });

    // 群资料还没同步过来（没有 lark_group_chat_info 那一行）时人数未知，只能看白名单。
    it('查不到群资料时只认白名单', async () => {
        const denied = rig({ groupChat: null });
        await denied.run();
        expect(denied.said.cards).toEqual([]);

        const allowed = rig({ groupChat: null, permission: { allow_send_pixiv_image: true } });
        await allowed.run();
        expect(allowed.said.cards).toHaveLength(1);
    });

    it('把 allow_send_limit_photo 递给取图那一步', async () => {
        const it_ = rig({
            groupChat: A_GROUP(5),
            permission: { allow_send_limit_photo: true },
        });

        await it_.run();

        expect(it_.said.asked).toEqual([expect.objectContaining({ status: StatusMode.NOT_DELETE })]);
    });
});

describe('回复', () => {
    it('卡片挂在触发它的那条消息上回复，不进话题', async () => {
        const it_ = rig({ groupChat: A_GROUP(5) });

        await it_.run();

        expect(it_.said.cards).toHaveLength(1);
        expect(it_.said.cards[0]!.messageId).toBe('om_1');
        expect(it_.said.cards[0]!.inThread).toBe(false);
    });

    // 出错的那一句反过来**进话题**，与拆分前一致。
    it('一张图都没搜到时把图库那句话转告用户', async () => {
        const it_ = rig({ groupChat: A_GROUP(5), photos: async () => [] });

        await it_.run();

        expect(it_.said.cards).toEqual([]);
        expect(it_.said.text).toEqual([
            { messageId: 'om_1', text: '没有找到图片', inThread: true },
        ]);
    });

    // 图库抛的不是 Error（或者根本没有 message）时也得说点什么，不能什么都不发。
    it('抛出来的不是 Error 时说一句兜底的', async () => {
        const it_ = rig({
            groupChat: A_GROUP(5),
            photos: async () => {
                throw 'mongo exploded';
            },
        });

        await it_.run();

        expect(it_.said.text[0]!.text).toBe(
            '呜呜...好像遇到奇怪的小问题了呢 (´;ω;｀) 要不稍后再试试？',
        );
    });

    // 飞书连那句道歉都收不下的时候，往上抛只会让引擎记一条 handler_error —— 用户
    // 已经无从补救了。与拆分前一致：那一句是发出去就不管的。
    it('连那句道歉都发不出去时不外溢', async () => {
        const it_ = rig({
            groupChat: A_GROUP(5),
            photos: async () => [],
            replyTextFails: true,
        });

        expect((await it_.run()).kind).not.toBe('handler_error');
    });
});
