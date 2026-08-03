import { describe, expect, it } from 'bun:test';

import { larkContentOf } from './lark-content';
import { resolveLarkMentions, type LarkBotLookup } from './mentions';
import { parseLarkMessage } from './parse-message';
import type { LarkMention, LarkMessageEvent } from './wire';

const noBots: LarkBotLookup = { byAppId: () => null, byUnionId: () => null };

function partsOf(
    messageType: string,
    content: string,
    mentions: LarkMention[] = [],
    bots: LarkBotLookup = noBots,
) {
    const event: LarkMessageEvent = {
        app_id: 'cli_app',
        sender: { sender_type: 'user', sender_id: { union_id: 'on_u', open_id: 'ou_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: messageType,
            content,
            mentions,
        },
    };
    const parsed = parseLarkMessage(event)!;
    return larkContentOf(parsed, resolveLarkMentions(parsed.mentions, bots));
}

describe('larkContentOf', () => {
    it('renders plain text as a single text part', () => {
        expect(partsOf('text', JSON.stringify({ text: 'hello' }))).toEqual([
            { type: 'text', value: 'hello' },
        ]);
    });

    // 飞书原生形态里 @ 是**独立的片段**，不是一串带 @ 的文字：规则引擎要按
    // 「这条 @ 了谁」判定，把它拍平成文本就再也认不出来了。
    it('keeps a mention as its own part, carrying who was mentioned', () => {
        const mentions: LarkMention[] = [
            { key: '@_user_1', id: { union_id: 'on_a' }, name: '张三' },
        ];
        expect(partsOf('text', JSON.stringify({ text: 'hi @_user_1 !' }), mentions)).toEqual([
            { type: 'text', value: 'hi ' },
            {
                type: 'mention',
                value: '张三',
                meta: { channel_user_id: 'on_a', bot_common_user_id: undefined },
            },
            { type: 'text', value: ' !' },
        ]);
    });

    it('marks a mention of one of our bots with its common user id', () => {
        const mentions: LarkMention[] = [
            {
                key: '@_user_1',
                id: { union_id: 'on_bot' },
                name: 'raw',
                mentioned_type: 'bot',
                bot_info: { app_id: 'cli_a' },
            },
        ];
        const bots: LarkBotLookup = {
            byAppId: () => ({ botName: 'chiwei', displayName: '赤尾', commonUserId: 'cu_1' }),
            byUnionId: () => null,
        };
        expect(partsOf('text', JSON.stringify({ text: '@_user_1' }), mentions, bots)).toEqual([
            {
                type: 'mention',
                value: '赤尾',
                meta: { channel_user_id: 'on_bot', bot_common_user_id: 'cu_1' },
            },
        ]);
    });

    // 正文里出现了一个 mentions 里没有的占位符：原样当文字留着，不猜。
    it('leaves an unmatched token as literal text', () => {
        expect(partsOf('text', JSON.stringify({ text: 'a @_user_7 b' }))).toEqual([
            { type: 'text', value: 'a ' },
            { type: 'text', value: '@_user_7' },
            { type: 'text', value: ' b' },
        ]);
    });

    it('renders an image part', () => {
        expect(partsOf('image', JSON.stringify({ image_key: 'img_1' }))).toEqual([
            { type: 'image', value: 'img_1' },
        ]);
    });

    it('renders a sticker part', () => {
        expect(partsOf('sticker', JSON.stringify({ file_key: 'stk_1' }))).toEqual([
            { type: 'sticker', value: 'stk_1' },
        ]);
    });

    // 领域里叫 video，落库的类型字面量必须仍是 media —— 那是已经写进历史消息的值。
    it('renders a video under the media type the message store already uses', () => {
        expect(
            partsOf(
                'media',
                JSON.stringify({
                    file_key: 'f_v',
                    image_key: 'i_v',
                    file_name: 'clip.mp4',
                    duration: 30,
                }),
            ),
        ).toEqual([
            {
                type: 'media',
                value: 'f_v',
                meta: { image_key: 'i_v', file_name: 'clip.mp4', duration: 30 },
            },
        ]);
    });

    it('renders a file part with its name', () => {
        expect(partsOf('file', JSON.stringify({ file_key: 'f_1', file_name: 'a.pdf' }))).toEqual([
            { type: 'file', value: 'f_1', meta: { file_name: 'a.pdf' } },
        ]);
    });

    it('renders an audio part with its duration', () => {
        expect(partsOf('audio', JSON.stringify({ file_key: 'f_a', duration: 5 }))).toEqual([
            { type: 'audio', value: 'f_a', meta: { duration: 5 } },
        ]);
    });

    it('renders an unsupported type with its placeholder and original name', () => {
        expect(partsOf('merge_forward', '{}')).toEqual([
            {
                type: 'unsupported',
                value: '[合并转发]',
                meta: { original_type: 'merge_forward' },
            },
        ]);
    });

    it('renders a rich-text post as alternating parts', () => {
        const post = {
            content: [
                [
                    { tag: 'text', text: 'hello ' },
                    { tag: 'img', image_key: 'img_p' },
                ],
                [{ tag: 'text', text: 'second' }],
            ],
        };
        expect(partsOf('post', JSON.stringify(post))).toEqual([
            { type: 'text', value: 'hello ' },
            { type: 'image', value: 'img_p' },
            { type: 'text', value: 'second' },
        ]);
    });
});
