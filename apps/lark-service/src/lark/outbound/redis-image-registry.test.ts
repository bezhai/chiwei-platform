import { describe, expect, it } from 'bun:test';

import { redisImageRegistry, type LarkImageRegistryStore } from './redis-image-registry';

function store(
    hashes: Record<string, Record<string, string> | null>,
): LarkImageRegistryStore & { keys: string[] } {
    const keys: string[] = [];
    return {
        keys,
        async hgetall(key) {
            keys.push(key);
            return hashes[key] as Record<string, string>;
        },
    };
}

describe('redisImageRegistry.lookup', () => {
    it('按 image_registry:{全局消息 id} 取整张表', async () => {
        // 这个前缀是**跨服务约定**：写的是 agent-service（Python），读的是这里。
        // 拼错的症状是每次都查不到、每张图都降级成"不可用"，而且没有任何报错。
        const s = store({ 'image_registry:global_msg_1': { '1.png': 'https://tos/1.png' } });

        expect(await redisImageRegistry(s).lookup('global_msg_1')).toEqual({
            '1.png': 'https://tos/1.png',
        });
        expect(s.keys).toEqual(['image_registry:global_msg_1']);
    });

    it('这条消息本来就没有图时给 null', async () => {
        const s = store({});
        expect(await redisImageRegistry(s).lookup('global_msg_1')).toBeNull();
    });

    it('空 hash 也算没有', async () => {
        // ioredis 对不存在的 key 返回 {} 而不是 null，两种都要当"没有"。
        const s = store({ 'image_registry:g1': {} });
        expect(await redisImageRegistry(s).lookup('g1')).toBeNull();
    });
});

describe('redisImageRegistry.download', () => {
    it('把响应体读成字节', async () => {
        const registry = redisImageRegistry(store({}), async () =>
            new Response(new Uint8Array([1, 2, 3])),
        );

        expect(await registry.download('https://tos/1.png')).toEqual(Buffer.from([1, 2, 3]));
    });

    it('HTTP 状态不对时抛，把状态码带上', async () => {
        // 抛出去由 images.ts 统一降级。这里不自己降级 —— 它才知道降级文案该说什么。
        const registry = redisImageRegistry(store({}), async () =>
            new Response('nope', { status: 403 }),
        );

        expect(registry.download('https://tos/1.png')).rejects.toThrow('403');
    });

    it('把要下的 URL 原样交给 fetch', async () => {
        const seen: string[] = [];
        const registry = redisImageRegistry(store({}), async (input) => {
            seen.push(String(input));
            return new Response(new Uint8Array([1]));
        });

        await registry.download('https://tos/deep/path.png?sig=abc');
        expect(seen).toEqual(['https://tos/deep/path.png?sig=abc']);
    });
});
