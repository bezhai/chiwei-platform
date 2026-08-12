// lark_emoji：飞书表情的 key，和它在输入法里显示的那个文本（`微笑` / `OK`）。
//
// 一张两列的表、两个动作、两个互不相干的调用方：
//
//     每小时的同步任务 ──replaceAllEmojis──▶ lark_emoji ──emojisByText──▶ 复读
//
// 写端是 emoji-sync（sync.ts），读端**只有复读**（../repeat/echo.ts）：用户说了
// `[微笑]`，赤尾要原样复读回去，就得先把这个文本换回飞书认的表情 key。这也是这张表
// 和复读为什么必须在同一批里搬 —— 分开搬的话写端没有可观测的读端、读端没有数据来源，
// 两边都没有可测的闭环。
//
// ## 为什么它不进 LarkStore
//
// LarkStore 描述的是「一条消息进来要读写哪些行」（见 ../projection/tables.ts 的文件
// 头），而这两个动作一个来自定时任务、一个来自指令，都不在投影那条链上。塞进去之后
// 那个端口就不再说明自己是什么了。
//
// ## 上游那几个方法故意没搬
//
// channel-server 的 LarkEmojiRepository 还有 getAllEmojis / getEmojiByKey /
// upsertEmojis / clearAllEmojis / deleteEmojisByKeys —— **全仓一个调用方都没有**
// （spec 的 caller coverage 一节点名了前两个，另外三个同理）。搬过来只是凭空多出几条
// 要维护的契约，以及一条 clearAllEmojis 那样"一句话清空整张表"的现成footgun。

import { In, Not, type DataSource } from 'typeorm';

import { LarkEmoji } from '../../entities/lark-emoji';

/** lark_emoji 的一行。字段名用物理列名，与端口层的其余部分一个口径。 */
export interface LarkEmojiRow {
    key: string;
    text: string;
}

export interface LarkEmojiCatalog {
    /**
     * 按显示文本查表情。**查不到的文本不会出现在结果里**，调用方据此降级
     * （复读把 `[没这个]` 当普通文字发出去，见 ../repeat/echo.ts）。
     */
    emojisByText(texts: readonly string[]): Promise<readonly LarkEmojiRow[]>;

    /**
     * 用远端的有效集合整体替换本地：有的覆盖、没有的删掉。
     *
     * **空集合是 no-op，不是"清空"。** 见真身里那段注释。
     */
    replaceAllEmojis(rows: readonly LarkEmojiRow[]): Promise<void>;
}

export function postgresEmojiCatalog(dataSource: DataSource): LarkEmojiCatalog {
    return {
        async emojisByText(texts): Promise<readonly LarkEmojiRow[]> {
            // 复读对**每条**群消息都问一次，其中绝大多数一个 `[xxx]` 都没有。空的 IN
            // 在 SQL 里没有意义，各版本 TypeORM 对它的处理也不一致 —— 直接不查。
            if (texts.length === 0) return [];

            const rows = await dataSource.getRepository(LarkEmoji).find({
                where: { text: In([...texts]) },
            });
            return rows.map((row) => ({ key: row.key, text: row.text }));
        },

        async replaceAllEmojis(rows): Promise<void> {
            // 下面那条 DELETE 的 NOT IN 在空集合上会匹配到**整张表**。远端一次空响应
            // （或者谁手滑传了个空数组）就等于把 lark_emoji 清空，而且全程不报错：
            // 同步照常打印成功，复读只是从此再也认不出任何表情。所以空集合在这里到此
            // 为止。调用方那侧还有一层（sync.ts 要打一条 warn），两处都留着。
            if (rows.length === 0) return;

            const keys = rows.map((row) => row.key);

            await dataSource.transaction(async (manager) => {
                const repository = manager.getRepository(LarkEmoji);
                // upsert 而不是「清空再插」：清空和重写之间的那一瞬间，并发的复读会
                // 查到一张空表。
                await repository.upsert([...rows], {
                    conflictPaths: ['key'],
                    skipUpdateIfNoValuesChanged: true,
                });
                // 远端已经没有的表情要跟着消失。留着的话，改过名的表情会剩下一条指向
                // 旧文本的僵尸行，复读按文本查就换出一个错的 key。
                await repository.delete({ key: Not(In(keys)) });
            });
        },
    };
}
