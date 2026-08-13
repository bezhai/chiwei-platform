// 一条飞书消息在规则层长什么样。
//
// 最要紧的一条在最后一个 describe：文本访问器建在**飞书原生片段**上，不是建在通用
// 契约的 content 上。两者对同一条消息给出不同的答案，选错了每条指令都会失配。

import { describe, expect, it } from 'bun:test';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkInboundProjection } from '../projection/inbound-projection';
import { larkRuleMessage } from './rule-message';

const BOT_COMMON_USER_ID = 'cu_bot_chiwei';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === 'cli_chiwei'
            ? { botName: 'chiwei', displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

function reading(overrides: Partial<LarkMessageEvent['message']> = {}): LarkMessageReading {
    const event: LarkMessageEvent = {
        app_id: 'cli_chiwei',
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: '{"text":"hi"}',
            ...overrides,
        },
    };
    const parsed = readLarkMessageEvent(event, bots);
    if (!parsed) throw new Error('test fixture is not a message event');
    return parsed;
}

/** 正文是 "@赤尾 余额"，@ 的是我们自己的 bot。 */
function mentioningTheBot(): LarkMessageReading {
    return reading({
        content: '{"text":"@_user_1 余额"}',
        mentions: [
            {
                key: '@_user_1',
                id: { union_id: 'on_bot_chiwei' },
                name: 'chiwei-raw',
                mentioned_type: 'bot',
                bot_info: { app_id: 'cli_chiwei' },
            },
        ],
    });
}

const projection: LarkInboundProjection = {
    commonUserId: 'cu_sender',
    commonConversationId: 'cc_1',
    commonMessageId: 'cm_1',
    commonRootMessageId: 'cm_root',
    commonReplyMessageId: 'cm_parent',
    mentionedCommonUserIds: [BOT_COMMON_USER_ID, 'cu_li'],
};

function ruleMessage(r: LarkMessageReading = reading()) {
    return larkRuleMessage(r, projection, {
        botName: 'chiwei',
        commonUserId: BOT_COMMON_USER_ID,
    });
}

describe('larkRuleMessage：公共层身份', () => {
    it('规则层只看公共层 id，一个飞书裸 id 都不带', () => {
        const message = ruleMessage();

        expect(message).toMatchObject({
            channel: 'lark',
            botName: 'chiwei',
            commonUserId: 'cu_sender',
            commonConversationId: 'cc_1',
            commonMessageId: 'cm_1',
            commonRootMessageId: 'cm_root',
            botCommonUserId: BOT_COMMON_USER_ID,
        });
        expect(JSON.stringify(message)).not.toContain('om_1');
        expect(JSON.stringify(message)).not.toContain('oc_1');
        expect(JSON.stringify(message)).not.toContain('ou_user');
    });

    // 群聊唯一的应答判据（NeedRobotMention）就是这一条：被 @ 的人里有没有我。
    it('被 @ 的人用投影产出的公共层 id，顺序与去重都照搬', () => {
        expect(ruleMessage().mentionedUserIds).toEqual([BOT_COMMON_USER_ID, 'cu_li']);
    });

    it('私聊标成 direct，群聊不是', () => {
        expect(ruleMessage(reading({ chat_type: 'p2p' })).isDirect).toBe(true);
        expect(ruleMessage(reading({ chat_type: 'group' })).isDirect).toBe(false);
    });

    it('createTime 取飞书的毫秒时间戳；读不成数记 0 而不是 NaN', () => {
        expect(ruleMessage().createTime).toBe(1700000000000);
        expect(ruleMessage(reading({ create_time: 'not-a-number' })).createTime).toBe(0);
    });
});

describe('larkRuleMessage：文本访问器建在飞书原生片段上', () => {
    // 本批最容易搞错的一处。通用契约那份 content 把 @ 内联进了文本，拿它建
    // clearText 的话，"@赤尾 余额" 会读成含名字的一串，`EqualText('余额')` 永远失配。
    it('clearText 不含被 @ 的名字', () => {
        expect(ruleMessage(mentioningTheBot()).clearText()).toBe('余额');
    });

    // 上一条要是拿通用契约建的，读到的会是这个。两者确实不同 —— 所以上一条不是空转。
    it('通用契约那份正文确实把名字内联了进去（所以上一条选的不是同一个东西）', () => {
        const inlined = mentioningTheBot()
            .inbound.content.map((item) => ('text' in item ? item.text : ''))
            .join('');
        expect(inlined).toContain('赤尾');
    });

    it('text() 把 @ 渲染成人在群里看到的样子', () => {
        expect(ruleMessage(mentioningTheBot()).text()).toBe('@赤尾 余额');
    });

    it('withoutEmojiText 去掉表情标记', () => {
        const message = ruleMessage(reading({ content: '{"text":"好的[微笑]"}' }));
        expect(message.withoutEmojiText()).toBe('好的');
    });

    it('isTextOnly 认纯文本与 @', () => {
        expect(ruleMessage(mentioningTheBot()).isTextOnly()).toBe(true);
        expect(
            ruleMessage(
                reading({ message_type: 'image', content: '{"image_key":"img_1"}' }),
            ).isTextOnly(),
        ).toBe(false);
    });

    it('表情包、图片的 key 从飞书原生片段取', () => {
        const sticker = ruleMessage(
            reading({ message_type: 'sticker', content: '{"file_key":"stk_1"}' }),
        );
        expect(sticker.isStickerOnly()).toBe(true);
        expect(sticker.stickerKey()).toBe('stk_1');

        const image = ruleMessage(
            reading({ message_type: 'image', content: '{"image_key":"img_1"}' }),
        );
        expect(image.imageKeys()).toEqual(['img_1']);
        expect(image.isStickerOnly()).toBe(false);
        expect(image.stickerKey()).toBe('');
    });
});
