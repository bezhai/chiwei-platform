import { describe, expect, it } from 'bun:test';

import {
    larkClearText,
    larkContentOf,
    larkFileKeys,
    larkImageKeys,
    larkIsStickerOnly,
    larkIsTextOnly,
    larkStickerKey,
    larkText,
    larkWithoutEmojiText,
    type LarkContentPart,
} from './lark-content';
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

// 规则判定读到的正文。**建在飞书原生片段上，不建在通用契约的 content 上** ——
// 后者把 @ 内联回了文本（"@赤尾 余额"），clearText 会留下那个名字，`EqualText('余额')`
// 这类指令从此全部失配。口径照拆分前的 MessageContentUtils 逐条重写。
function parts(...items: LarkContentPart[]): LarkContentPart[] {
    return items;
}

const text = (value: string): LarkContentPart => ({ type: 'text', value });
const mention = (value: string): LarkContentPart => ({ type: 'mention', value, meta: {} });

describe('larkClearText', () => {
    // 指令匹配用的就是这一个。@ 片段整段不算数 —— 群里必须 @ 机器人才说得上话，
    // 把名字混进正文的话每条指令都得先想办法把它摘掉。
    it('keeps only the literal text, dropping mentions entirely', () => {
        expect(larkClearText(parts(mention('赤尾'), text(' 余额')))).toBe('余额');
    });

    it('collapses runs of whitespace and trims the ends', () => {
        expect(larkClearText(parts(text('  查   一下\n余额  ')))).toBe('查 一下 余额');
    });

    // 正文里没对上 mention 记录的占位符会原样留成文字（见 larkContentOf）。
    // 它不是用户写的字，不能进指令匹配。
    it('strips a mention token that stayed literal', () => {
        expect(larkClearText(parts(text('@_user_7 余额')))).toBe('余额');
    });

    // 非文字片段整段不参与，被它隔开的两段文字直接相接（拆分前就是这样拼的）。
    it('ignores images, stickers and every other non-text part', () => {
        expect(
            larkClearText(parts(text('看'), { type: 'image', value: 'img_1' }, text('这个'))),
        ).toBe('看这个');
    });

    it('reads an empty content as an empty string', () => {
        expect(larkClearText([])).toBe('');
    });
});

describe('larkText', () => {
    // 关键词匹配读的是这一个：@ 渲染成 "@显示名"，与人在群里看到的一致。
    it('renders a mention inline as @display-name', () => {
        expect(larkText(parts(mention('赤尾'), text(' 在吗')))).toBe('@赤尾 在吗');
    });

    it('leaves whitespace alone', () => {
        expect(larkText(parts(text('  a  b  ')))).toBe('  a  b  ');
    });

    it('skips non-text parts', () => {
        expect(larkText(parts(text('a'), { type: 'sticker', value: 'stk' }, text('b')))).toBe('ab');
    });
});

describe('larkWithoutEmojiText', () => {
    // [xxx] 和 <xxx> 是飞书的表情与富文本标记。它们不该参与文本匹配。
    it('drops bracketed emoji markers from the cleared text', () => {
        expect(larkWithoutEmojiText(parts(text('好的[微笑]<at>')))).toBe('好的');
    });

    it('clears the text first, so mentions never come back', () => {
        expect(larkWithoutEmojiText(parts(mention('赤尾'), text(' 好 [笑]')))).toBe('好 ');
    });
});

describe('larkIsTextOnly', () => {
    it('is true when every part is text or a mention', () => {
        expect(larkIsTextOnly(parts(mention('赤尾'), text(' hi')))).toBe(true);
    });

    it('is false as soon as one part is not', () => {
        expect(larkIsTextOnly(parts(text('hi'), { type: 'image', value: 'img_1' }))).toBe(false);
    });

    // 空正文算"纯文本"。这是飞书拆分前的口径（`every` 对空数组为真），QQ 那侧另外
    // 要求 length > 0 —— 两个渠道本来就不一致，这里按飞书那份走。
    it('is true for an empty content, matching the pre-split Lark behaviour', () => {
        expect(larkIsTextOnly([])).toBe(true);
    });
});

describe('larkIsStickerOnly', () => {
    it('is true only when the whole message is one sticker', () => {
        expect(larkIsStickerOnly(parts({ type: 'sticker', value: 'stk' }))).toBe(true);
    });

    it('is false when the sticker comes with anything else', () => {
        expect(larkIsStickerOnly(parts({ type: 'sticker', value: 'stk' }, text('哈')))).toBe(false);
        expect(larkIsStickerOnly([])).toBe(false);
    });
});

describe('larkStickerKey / larkImageKeys', () => {
    it('answers with the first sticker key, or an empty string when there is none', () => {
        expect(larkStickerKey(parts(text('a'), { type: 'sticker', value: 'stk_1' }))).toBe('stk_1');
        expect(larkStickerKey(parts(text('a')))).toBe('');
    });

    it('answers with every image key, in order', () => {
        expect(
            larkImageKeys(
                parts(
                    { type: 'image', value: 'img_1' },
                    text('a'),
                    { type: 'image', value: 'img_2' },
                    // 视频的封面图落在 media 片段的 meta 里，不是一张图片。
                    { type: 'media', value: 'f_v', meta: { image_key: 'i_v' } },
                ),
            ),
        ).toEqual(['img_1', 'img_2']);
    });
});

describe('larkFileKeys', () => {
    it('answers with every file key, in order', () => {
        expect(
            larkFileKeys(
                parts(
                    { type: 'file', value: 'file_1', meta: { file_name: '一.txt' } },
                    text('a'),
                    { type: 'file', value: 'file_2', meta: { file_name: '二.epub' } },
                ),
            ),
        ).toEqual(['file_1', 'file_2']);
    });

    // 附件缓存的两条轨按片段类型分流：视频（media）和图片（image）不进文件轨。
    // 混进来的后果是文件管线拿着一个视频 file_key 去下载，存进对象存储的东西对不上
    // 「读小说」那类调用方要的形状。
    it('leaves videos and images to the other track', () => {
        expect(
            larkFileKeys(
                parts(
                    { type: 'image', value: 'img_1' },
                    { type: 'media', value: 'f_v', meta: { image_key: 'i_v' } },
                    { type: 'audio', value: 'f_a', meta: {} },
                    { type: 'sticker', value: 'stk' },
                ),
            ),
        ).toEqual([]);
    });

    it('is empty when the message carries no file at all', () => {
        expect(larkFileKeys(parts(text('hi')))).toEqual([]);
    });
});
