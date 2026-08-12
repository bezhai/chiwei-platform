// 每小时那次 emoji 同步。
//
// 这个任务的失效方式全是安静的：把 isDeleted 的表情也写进去，复读会用一个已经下架的
// key 发消息（飞书渲染成一个方框）；远端返回空却照样往下写，lark_emoji 被清空、复读
// 从此认不出任何表情。两种都不会报错，也不会有人立刻发现。所以这几条都要钉住。

import { describe, expect, it } from 'bun:test';

import type { LarkEmojiRow } from './catalog';
import {
    LARK_EMOJI_FEED_URL,
    httpEmojiSource,
    syncLarkEmojis,
    type LarkEmojiFeed,
} from './sync';

function feed(
    entries: Array<{ key: string; text: string; isDeleted: boolean }>,
): LarkEmojiFeed {
    return {
        emojiData: Object.fromEntries(
            entries.map((entry) => [
                entry.key,
                { ...entry, imageKey: `img_${entry.key}` },
            ]),
        ),
    };
}

function rig(source: () => Promise<LarkEmojiFeed>) {
    const written: LarkEmojiRow[][] = [];
    const run = syncLarkEmojis({
        source,
        catalog: {
            replaceAllEmojis: async (rows) => {
                written.push([...rows]);
            },
        },
    });
    return { run, written };
}

describe('emoji-sync', () => {
    it('只写还活着的表情，而且只写 key 和 text 两列', async () => {
        const { run, written } = rig(async () =>
            feed([
                { key: 'SMILE', text: '微笑', isDeleted: false },
                { key: 'GONE', text: '已下架', isDeleted: true },
                { key: 'OK', text: 'OK', isDeleted: false },
            ]),
        );

        await run();

        expect(written).toEqual([
            [
                { key: 'SMILE', text: '微笑' },
                { key: 'OK', text: 'OK' },
            ],
        ]);
    });

    // 远端整个挂了、或者刚好返回一批全是 isDeleted 的数据。往下写就是一次整表清空
    // （replaceAllEmojis 的 NOT IN 在空集合上匹配整张表），而复读会安静地不再认表情。
    it('一个有效表情都没有时不碰库', async () => {
        const { run, written } = rig(async () => feed([{ key: 'GONE', text: 'x', isDeleted: true }]));

        await run();

        expect(written).toEqual([]);
    });

    it('远端为空（连 emojiData 都没有）时同样不碰库', async () => {
        const { run, written } = rig(async () => ({ emojiData: {} }));

        await run();

        expect(written).toEqual([]);
    });

    // 抛出去而不是吞掉：schedule.ts 的基座会把它记成一条 failed。吞掉的话，一个连着
    // 几周同步失败的进程看上去和正常的一模一样。
    it('远端出错往上抛', async () => {
        const { run, written } = rig(async () => {
            throw new Error('emoji api is down');
        });

        expect(run()).rejects.toThrow('emoji api is down');
        expect(written).toEqual([]);
    });
});

describe('远端来源', () => {
    it('打的是那个写死的地址，body 原样交出去', async () => {
        const asked: string[] = [];
        const source = httpEmojiSource((async (url: string) => {
            asked.push(String(url));
            return new Response(JSON.stringify(feed([{ key: 'OK', text: 'OK', isDeleted: false }])));
        }) as unknown as typeof fetch);

        const body = await source();

        expect(asked).toEqual([LARK_EMOJI_FEED_URL]);
        expect(Object.keys(body.emojiData)).toEqual(['OK']);
    });

    // fetch 对 4xx/5xx **不抛**，它只是把状态码放在 res.ok 上。不检查的话
    // `res.json()` 会拿一段错误页去解析，最后变成一个跟"emoji 服务挂了"完全对不上的
    // 报错（或者更糟：解析成功、emojiData 为空、走进上面那条"不碰库"分支静默跳过）。
    it('非 2xx 抛，错误里带上状态码', async () => {
        const source = httpEmojiSource((async () =>
            new Response('nope', { status: 503 })) as unknown as typeof fetch);

        expect(source()).rejects.toThrow(/503/);
    });
});
