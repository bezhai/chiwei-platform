// 把用户刚说的那条消息重新拼成一条**发得出去**的飞书富文本。
//
// 两步各有各的坑：
//
//   * `<at>` 标签丢了 union_id，复读出来就是一个光秃秃的 "@"，读的人不知道 @ 的是谁；
//   * `[微笑]` 换不出 key 却照样发 emotion 节点，飞书会把整条消息拒收 —— 一个查不到的
//     表情让整次复读消失。所以查不到必须降级成普通文字。

import { describe, expect, it } from 'bun:test';

import type { LarkContentPart } from '../message/lark-content';
import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkEmojiCatalog, LarkEmojiRow } from '../emoji/catalog';
import { echoPostContent, larkAtTaggedText } from './echo';

// ---------------------------------------------------------------------------

function catalogOf(rows: LarkEmojiRow[]): {
    catalog: Pick<LarkEmojiCatalog, 'emojisByText'>;
    asked: string[][];
} {
    const asked: string[][] = [];
    return {
        asked,
        catalog: {
            emojisByText: async (texts) => {
                asked.push([...texts]);
                return rows.filter((row) => texts.includes(row.text));
            },
        },
    };
}

const SMILE: LarkEmojiRow = { key: 'SMILE', text: '微笑' };

// ---------------------------------------------------------------------------

describe('正文 → 带 <at> 标签的文本', () => {
    it('被 @ 的人写成飞书认的标签，其余片段原样拼回去', () => {
        const parts: LarkContentPart[] = [
            { type: 'text', value: '早上好 ' },
            { type: 'mention', value: '张三', meta: { channel_user_id: 'on_zhang' } },
            { type: 'text', value: ' 今天也来啦' },
        ];

        expect(larkAtTaggedText(parts)).toBe('早上好 <at user_id="on_zhang"></at> 今天也来啦');
    });

    // 拿不到 union_id 的 mention（飞书偶尔只给 open_id）退回文字形式。发一个
    // `user_id=""` 的标签飞书会拒收整条消息。
    it('没有 union_id 的 @ 退回 "@显示名"', () => {
        const parts: LarkContentPart[] = [
            { type: 'mention', value: '李四', meta: {} },
            { type: 'text', value: ' 在吗' },
        ];

        expect(larkAtTaggedText(parts)).toBe('@李四 在吗');
    });

    it('meta 里是空串也退回 "@显示名"', () => {
        const parts: LarkContentPart[] = [
            { type: 'mention', value: '李四', meta: { channel_user_id: '' } },
        ];

        expect(larkAtTaggedText(parts)).toBe('@李四');
    });

    // 端到端走一遍真的解析：mention 的 union_id 到底有没有落进 meta.channel_user_id，
    // 手搓的片段说明不了。
    it('从真的飞书事件解析出来的正文也接得上', () => {
        const bots: LarkBotLookup = { byAppId: () => null, byUnionId: () => null };
        const event: LarkMessageEvent = {
            app_id: 'cli_x',
            sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
            message: {
                message_id: 'om_1',
                chat_id: 'oc_1',
                chat_type: 'group',
                create_time: '1700000000000',
                message_type: 'text',
                content: '{"text":"@_user_1 早"}',
                mentions: [{ key: '@_user_1', id: { union_id: 'on_zhang' }, name: '张三' }],
            },
        };

        const reading = readLarkMessageEvent(event, bots)!;

        expect(larkAtTaggedText(reading.content)).toBe('<at user_id="on_zhang"></at> 早');
    });
});

describe('文本 → 飞书富文本', () => {
    it('纯文字就是一个 text 节点', async () => {
        const { catalog } = catalogOf([]);

        expect(await echoPostContent(catalog, '早上好')).toEqual({
            content: [[{ tag: 'text', text: '早上好' }]],
        });
    });

    it('<at> 标签变成 at 节点，前后的字各成一个 text 节点', async () => {
        const { catalog } = catalogOf([]);

        expect(await echoPostContent(catalog, '早 <at user_id="on_zhang"></at> 好')).toEqual({
            content: [
                [
                    { tag: 'text', text: '早 ' },
                    { tag: 'at', user_id: 'on_zhang' },
                    { tag: 'text', text: ' 好' },
                ],
            ],
        });
    });

    it('查得到的 [表情] 变成 emotion 节点，装的是 key 不是文本', async () => {
        const { catalog } = catalogOf([SMILE]);

        expect(await echoPostContent(catalog, '哈哈[微笑]')).toEqual({
            content: [
                [
                    { tag: 'text', text: '哈哈' },
                    { tag: 'emotion', emoji_type: 'SMILE' },
                ],
            ],
        });
    });

    // 降级：查不到就当普通文字。发一个 emoji_type 对不上的 emotion 节点，飞书会拒收
    // **整条**消息 —— 一个不认识的表情让整次复读消失，而且只在日志里留一条发送失败。
    it('查不到的 [表情] 原样留成文字', async () => {
        const { catalog } = catalogOf([SMILE]);

        expect(await echoPostContent(catalog, '哈哈[没这个]')).toEqual({
            content: [
                [
                    { tag: 'text', text: '哈哈' },
                    { tag: 'text', text: '[没这个]' },
                ],
            ],
        });
    });

    it('只问一次库，问的正好是文本里出现的那几个 [xxx]', async () => {
        const { catalog, asked } = catalogOf([SMILE]);

        await echoPostContent(catalog, '[微笑]中间[没这个]结尾');

        expect(asked).toEqual([['微笑', '没这个']]);
    });

    // 纯空白的片段被丢掉是拆分前的形状（`if (textBefore.trim())`），照搬。
    it('节点之间只有空白时不产出 text 节点', async () => {
        const { catalog } = catalogOf([SMILE]);

        expect(await echoPostContent(catalog, '<at user_id="on_z"></at> [微笑]')).toEqual({
            content: [[{ tag: 'at', user_id: 'on_z' }, { tag: 'emotion', emoji_type: 'SMILE' }]],
        });
    });

    // 一个节点都产出不了（空串、纯空白）时兜一个装着原文的 text 节点：飞书不收空
    // content，发出去是一次报错而不是一条空消息。
    it('一个节点都没有时兜一个装原文的 text 节点', async () => {
        const { catalog } = catalogOf([]);

        expect(await echoPostContent(catalog, '   ')).toEqual({
            content: [[{ tag: 'text', text: '   ' }]],
        });
    });
});
