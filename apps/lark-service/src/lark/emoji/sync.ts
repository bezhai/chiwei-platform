// `emoji-sync` 的任务本体：每小时把飞书表情表从远端拉一遍，整体替换本地。
//
// 挂在哪、什么时候跑、以谁的身份跑，全在 src/schedule.ts（与两个图片日报同一个基座）。
//
// ## 为什么它必须待在单副本那个进程里
//
// 与日报的理由不同：日报是"发两遍消息"，这个是"每个副本各按小时全量覆写同一张共享
// 表"。写的是同一份数据、结果也一样，所以不会有人发现 —— 只是白白多打几倍的远端请求
// 和数据库事务。lane gate（非 prod 部署不起）挡的是另一件事：泳道跑起来会把 prod 的
// lark_emoji 覆写掉。
//
// ## 远端是一个外部服务，不是我们自己的
//
// 地址写死，拆分前就是字面量（channel-server 的 crontab/services/emoji.ts），没有任何
// 配置来源。参数化是新设计不是迁移，照搬。
//
// 打它走裸 `fetch` 而不是 LaneRouter：那个路由器是给**本集群内部**的服务用的（它按
// 注册表拼 `{app}-{lane}` 的主机名、注入 x-ctx-lane），拿它去打一个外部域名没有意义。

import type { LarkEmojiCatalog, LarkEmojiRow } from './catalog';

/** 远端返回的一个表情。字段名是远端的口径，不是我们的列名。 */
export interface LarkEmojiFeedEntry {
    key: string;
    text: string;
    /** 已经下架的表情。飞书仍然会返回它们，但拿它发消息只会渲染成一个方框。 */
    isDeleted: boolean;
}

/** 远端一次响应。除了 emojiData 之外的字段（imageKey / skinKeys / easterEgg…）我们不用。 */
export interface LarkEmojiFeed {
    emojiData: Record<string, LarkEmojiFeedEntry>;
}

/** 从哪儿拿这份表。测试传替身 —— 开发机打不到那个域名。 */
export type LarkEmojiSource = () => Promise<LarkEmojiFeed>;

/** 拆分前就写死在代码里的那个地址。 */
export const LARK_EMOJI_FEED_URL = 'https://ywh-emoji-bot.fn-boe.bytedance.net/api/emojis';

/** 上游那个 axios 客户端的超时是 30 秒，照搬 —— 定时任务不该无限期挂在一次请求上。 */
const FEED_TIMEOUT_MS = 30_000;

export function httpEmojiSource(fetchImpl: typeof fetch = fetch): LarkEmojiSource {
    return async () => {
        const response = await fetchImpl(LARK_EMOJI_FEED_URL, {
            signal: AbortSignal.timeout(FEED_TIMEOUT_MS),
        });
        // fetch 对 4xx/5xx 不抛，只把状态码放在 ok 上。不看它的话，一段 HTML 错误页会
        // 被拿去 JSON.parse —— 运气不好时还能解析成功、emojiData 为空，于是同步"成功"
        // 地什么也没做。
        if (!response.ok) {
            throw new Error(
                `lark emoji feed answered ${response.status} ${response.statusText}`.trim(),
            );
        }
        return (await response.json()) as LarkEmojiFeed;
    };
}

export interface LarkEmojiSyncDeps {
    source: LarkEmojiSource;
    catalog: Pick<LarkEmojiCatalog, 'replaceAllEmojis'>;
}

export function syncLarkEmojis(deps: LarkEmojiSyncDeps): () => Promise<void> {
    return async () => {
        const feed = await deps.source();
        const all = Object.values(feed.emojiData ?? {});
        const alive: LarkEmojiRow[] = all
            .filter((entry) => !entry.isDeleted)
            .map((entry) => ({ key: entry.key, text: entry.text }));

        console.info(`[lark-emoji] fetched ${all.length} emoji(s), ${alive.length} still alive`);

        // 一个有效表情都没有 = 远端出了问题（整批下架是不会发生的）。往下写就是把
        // lark_emoji 清空，而复读只会安静地不再认表情。
        if (alive.length === 0) {
            console.warn('[lark-emoji] the feed has no live emoji; leaving lark_emoji untouched');
            return;
        }

        await deps.catalog.replaceAllEmojis(alive);
        console.info(`[lark-emoji] lark_emoji now holds ${alive.length} emoji(s)`);
    };
}
