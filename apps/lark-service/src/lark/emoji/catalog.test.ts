// lark_emoji 那两条语句真的长什么样。
//
// 这张表只有两个动作，而其中一个（replaceAllEmojis）是**先覆盖再删掉多余的**——
// 删除那半截的 `NOT IN` 一旦少了 WHERE 或者拿到空集合，就是一次整表清空。而清空之后
// 没有任何报错：同步任务照常打印"成功"，复读只是从此再也认不出 `[微笑]`。所以这两条
// 语句必须被单独钉住，而不是靠内存实现"看起来对"。
//
// 开发机连不到库，用不连库的录音 DataSource 拿 TypeORM 真的生成的 SQL
// （见 ../recording-data-source.ts）。

import { beforeEach, describe, expect, it } from 'bun:test';

import { LARK_SERVICE_ENTITIES } from '../../ormconfig';
import { recordingDataSource, type RecordedStatement } from '../recording-data-source';
import { postgresEmojiCatalog } from './catalog';
import type { LarkEmojiCatalog } from './catalog';

interface Harness {
    catalog: LarkEmojiCatalog;
    recorded: RecordedStatement[];
    reply(rows: Array<Record<string, unknown>>): void;
    sqlOf(fragment: string): RecordedStatement;
    statements(): string[];
}

function harness(): Harness {
    const recorder = recordingDataSource([...LARK_SERVICE_ENTITIES]);
    return { ...recorder, catalog: postgresEmojiCatalog(recorder.dataSource) };
}

let h: Harness;
beforeEach(() => {
    h = harness();
});

describe('读：按显示文本查表情 key', () => {
    it('按 text IN (...) 查，映射成列名口径的行', async () => {
        h.reply([
            { LarkEmoji_key: 'SMILE', LarkEmoji_text: '微笑' },
            { LarkEmoji_key: 'OK', LarkEmoji_text: 'OK' },
        ]);

        const rows = await h.catalog.emojisByText(['微笑', 'OK', '查不到的']);

        expect(rows).toEqual([
            { key: 'SMILE', text: '微笑' },
            { key: 'OK', text: 'OK' },
        ]);
        const select = h.sqlOf('FROM "lark_emoji"');
        expect(select.sql).toContain('"text" IN (');
        expect(select.params).toEqual(['微笑', 'OK', '查不到的']);
    });

    // 一条不含 `[xxx]` 的消息会带着空数组进来（复读对每条消息都问一次）。空的 IN
    // 在 SQL 里没有意义，而且各版本 TypeORM 对它的处理并不一致 —— 直接不查。
    it('一个都不问的时候不发语句', async () => {
        expect(await h.catalog.emojisByText([])).toEqual([]);
        expect(h.statements()).toEqual([]);
    });
});

describe('写：用远端的有效集合整体替换本地', () => {
    it('一个事务里先 upsert 再删掉不在集合里的行', async () => {
        await h.catalog.replaceAllEmojis([
            { key: 'SMILE', text: '微笑' },
            { key: 'OK', text: 'OK' },
        ]);

        expect(h.statements()[0]).toBe('begin');
        expect(h.statements().at(-1)).toBe('commit');

        // upsert：主键冲突就改 text。用 upsert 而不是 clear + insert —— 清空和重写
        // 之间的那一瞬间，复读会查不到任何表情。
        const insert = h.sqlOf('INSERT INTO "lark_emoji"');
        expect(insert.sql).toContain('ON CONFLICT');
        expect(insert.sql).toContain('"key"');
        expect(insert.params).toEqual(expect.arrayContaining(['SMILE', '微笑', 'OK', 'OK']));

        // 删除：远端已经没有的表情要跟着消失，否则改过名的表情会留下一条指向旧文本
        // 的僵尸行，复读拿它去查就换出一个错的 key。
        const remove = h.sqlOf('DELETE FROM "lark_emoji"');
        expect(remove.sql).toContain('WHERE NOT("key" IN (');
        expect(remove.params).toEqual(['SMILE', 'OK']);
    });

    // 这是最后一道防线：远端一次空响应（或者被人手工调用）会让上面那条 NOT IN
    // 匹配到整张表，一句话就把 lark_emoji 清空，而且全程不报错。
    it('空集合是 no-op —— 绝不拿它当"清空整张表"用', async () => {
        await h.catalog.replaceAllEmojis([]);
        expect(h.statements()).toEqual([]);
    });
});
