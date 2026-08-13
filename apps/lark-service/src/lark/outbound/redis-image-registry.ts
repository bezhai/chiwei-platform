// LarkImageRegistry 的真身：注册表在 Redis，图片本体在对象存储。
//
// **key 的拼法是跨服务、跨语言的约定**：写的是 agent-service（Python，产出图片的那
// 一侧），读的是这里。两边各写一个字面量，编译期对不上，所以这个前缀在本文件里只
// 出现一次，并且有测试钉着。拼错的症状是每次查空、每张图降级成"不可用"，全程无报错。
//
// 用来查的 id 必须是**全局消息 id**，不是飞书那边的裸 message id —— 上游按全局 id
// 注册，用裸 id 查必定 miss。这一点在端口注释里也写了，因为它是最容易接错的一处。

import type { LarkImageRegistry } from './images';

/** Redis 那一面只用到一个命令。泳道前缀由共享的 Redis 客户端自己加，这里不管。 */
export interface LarkImageRegistryStore {
    hgetall(key: string): Promise<Record<string, string>>;
}

/**
 * 下载那一面用到的 HTTP 表面，就这三样。
 *
 * 不写成 `typeof fetch`：那个类型上还挂着 preconnect 之类的运行时私货，替身得跟着
 * 编一份才通得过编译，而它们跟下载图片一点关系都没有。
 */
export type LarkImageFetch = (url: string) => Promise<{
    ok: boolean;
    status: number;
    arrayBuffer(): Promise<ArrayBuffer>;
}>;

const KEY_PREFIX = 'image_registry:';

export function redisImageRegistry(
    store: LarkImageRegistryStore,
    fetchImage: LarkImageFetch = fetch,
): LarkImageRegistry {
    return {
        async lookup(registryId) {
            const hash = await store.hgetall(`${KEY_PREFIX}${registryId}`);
            // ioredis 对不存在的 key 返回 {}，不是 null。两种都是"这条消息没有图"。
            if (!hash || Object.keys(hash).length === 0) return null;
            return hash;
        },

        async download(url) {
            const response = await fetchImage(url);
            if (!response.ok) {
                // 抛出去，由 images.ts 统一降级 —— 只有它知道降级文案该说什么，也只有
                // 它知道这张图挂了不该连累整条回复。
                throw new Error(`failed to download image: HTTP ${response.status}`);
            }
            return Buffer.from(await response.arrayBuffer());
        },
    };
}
