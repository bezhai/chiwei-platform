// 飞书口径的一段文字 → 发得出去的飞书富文本。
//
// 两条出站路（赤尾的回复、复读）都走这一个函数，所以这里的每条用例对两边同时成立。
// 最要紧的两条护栏：
//
//   * `[微笑]` 换不出 key 却照样发 emotion 节点，飞书会把**整条**消息拒收 —— 查不到
//     必须降级成普通文字；
//   * 正文是原话，里面的 `*` `_` `` ` `` 一个都不能被当成格式吃掉 —— 所以永远不产出
//     md 节点。

import { describe, expect, it } from 'bun:test';

import type { LarkEmojiCatalog, LarkEmojiRow } from '../emoji/catalog';
import { larkTextToPostContent } from './post-content';

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
// 飞书自己的表情 key 也有纯数字的，照原样装进 emotion 节点。
const HUMPH: LarkEmojiRow = { key: '7164219805602873347', text: '右哼哼' };

describe('文字 → text / at / emotion 节点', () => {
    it('纯文字就是一个 text 节点', async () => {
        const { catalog } = catalogOf([]);

        expect(await larkTextToPostContent(catalog, '早上好')).toEqual({
            content: [[{ tag: 'text', text: '早上好' }]],
        });
    });

    it('查得到的 [表情] 变成 emotion 节点，装的是 key 不是文本', async () => {
        const { catalog } = catalogOf([HUMPH]);

        expect(await larkTextToPostContent(catalog, '某人摸鱼摸得很起劲嘛[右哼哼]')).toEqual({
            content: [
                [
                    { tag: 'text', text: '某人摸鱼摸得很起劲嘛' },
                    { tag: 'emotion', emoji_type: '7164219805602873347' },
                ],
            ],
        });
    });

    // 发一个 emoji_type 对不上的 emotion 节点，飞书拒收**整条**消息。
    it('查不到的 [xxx] 原样留成文字', async () => {
        const { catalog } = catalogOf([SMILE]);

        expect(await larkTextToPostContent(catalog, '区间是[0, +∞]')).toEqual({
            content: [
                [
                    { tag: 'text', text: '区间是' },
                    { tag: 'text', text: '[0, +∞]' },
                ],
            ],
        });
    });

    it('<at> 标签变成 at 节点，标签里写没写名字都认', async () => {
        const { catalog } = catalogOf([]);

        // 带名字的是出站 mention 解析写的，空的是复读写的。
        expect(
            await larkTextToPostContent(
                catalog,
                '<at user_id="on_xm">小明</at> 和 <at user_id="on_zs"></at> 在吗',
            ),
        ).toEqual({
            content: [
                [
                    { tag: 'at', user_id: 'on_xm' },
                    { tag: 'text', text: ' 和 ' },
                    { tag: 'at', user_id: 'on_zs' },
                    { tag: 'text', text: ' 在吗' },
                ],
            ],
        });
    });

    it('名字里带 [xxx] 的 @ 整个是一个 at 节点，里面的方括号不去查表情', async () => {
        const { catalog, asked } = catalogOf([SMILE]);

        expect(await larkTextToPostContent(catalog, '<at user_id="on_x">[微笑]本人</at>[微笑]')).toEqual({
            content: [[{ tag: 'at', user_id: 'on_x' }, { tag: 'emotion', emoji_type: 'SMILE' }]],
        });
        expect(asked).toEqual([['微笑']]);
    });

    it('只问一次库，问的正好是文本里出现的那几个 [xxx]', async () => {
        const { catalog, asked } = catalogOf([SMILE]);

        await larkTextToPostContent(catalog, '[微笑]中间[没这个]\n下一行[微笑]');

        expect(asked).toEqual([['微笑', '没这个', '微笑']]);
    });

    it('套着的方括号：里层那个才是表情', async () => {
        const { catalog } = catalogOf([SMILE]);

        expect(await larkTextToPostContent(catalog, '[[微笑]]')).toEqual({
            content: [
                [
                    { tag: 'text', text: '[' },
                    { tag: 'emotion', emoji_type: 'SMILE' },
                    { tag: 'text', text: ']' },
                ],
            ],
        });
    });

    // 空格是原话的一部分：`[foo] [bar]` 丢掉中间那个空格就成了 `[foo][bar]`。
    it('节点之间的空格照原样留着', async () => {
        const { catalog } = catalogOf([SMILE]);

        expect(await larkTextToPostContent(catalog, '<at user_id="on_z"></at> [微笑]')).toEqual({
            content: [
                [
                    { tag: 'at', user_id: 'on_z' },
                    { tag: 'text', text: ' ' },
                    { tag: 'emotion', emoji_type: 'SMILE' },
                ],
            ],
        });
        expect(await larkTextToPostContent(catalog, '[foo] [bar]')).toEqual({
            content: [
                [
                    { tag: 'text', text: '[foo]' },
                    { tag: 'text', text: ' ' },
                    { tag: 'text', text: '[bar]' },
                ],
            ],
        });
    });

    // 表情是点缀，跟图一样：查不了就降级，不能让她那句话跟着失败。抛出去的话出站那侧
    // 会把它当成"可能已经发出去了"吞掉 —— 那句话就真的没了。
    it('表情表查不了：一个都不换，原样当文字发，不抛', async () => {
        const catalog = {
            emojisByText: async (): Promise<never> => {
                throw new Error('pg is down');
            },
        };

        expect(await larkTextToPostContent(catalog, '摸鱼是吧[微笑]')).toEqual({
            content: [
                [
                    { tag: 'text', text: '摸鱼是吧' },
                    { tag: 'text', text: '[微笑]' },
                ],
            ],
        });
    });
});

describe('分行', () => {
    // 飞书自己把一条多行消息编码成「一行一个段落」，段落之间的空行不单独占一行。
    it('每个非空行一个段落，空行不占段落', async () => {
        const { catalog } = catalogOf([SMILE]);

        expect(await larkTextToPostContent(catalog, '第一行[微笑]\n\n第二行\n   \n第三行')).toEqual({
            content: [
                [
                    { tag: 'text', text: '第一行' },
                    { tag: 'emotion', emoji_type: 'SMILE' },
                ],
                [{ tag: 'text', text: '第二行' }],
                [{ tag: 'text', text: '第三行' }],
            ],
        });
    });

    // 飞书不收空 content：发出去是一次报错而不是一条空消息。
    it('一个节点都没有时兜一个装原文的 text 节点', async () => {
        const { catalog } = catalogOf([]);

        expect(await larkTextToPostContent(catalog, '')).toEqual({
            content: [[{ tag: 'text', text: '' }]],
        });
        expect(await larkTextToPostContent(catalog, ' \n ')).toEqual({
            content: [[{ tag: 'text', text: ' \n ' }]],
        });
    });
});

describe('原话里的符号一个都不当格式解释', () => {
    it('颜表情里的 _ ` * 原样留在 text 节点里', async () => {
        const { catalog } = catalogOf([]);
        const said = '抢救回来一口气_(´ཀ`」 ∠)_ 哼！(◦`~´◦) **不是加粗**';

        expect(await larkTextToPostContent(catalog, said)).toEqual({
            content: [[{ tag: 'text', text: said }]],
        });
    });

    // image_key 只可能来自我们自己那次上传（见 pictures.ts）。正文里的图片语法是模型
    // 自己写的，变成 img 节点的话飞书查无此 key，拒收整条消息。
    it('正文里的图片语法就是一串字，变不出 img 节点', async () => {
        const { catalog } = catalogOf([]);
        const said = '看 ![photo](img_v3_abc) 和 ![](https://x.example/p.png)';

        const post = await larkTextToPostContent(catalog, said);

        expect(post.content.flat().some((node) => node.tag === 'img')).toBe(false);
        expect(post.content.flat().map((node) => node.tag)).not.toContain('md');
    });
});
