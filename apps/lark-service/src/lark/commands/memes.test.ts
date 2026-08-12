// 表情包服务那两个端点，以及模板列表那层十分钟缓存。
//
// 缓存那一段是整个 D4 里唯一一处**跨服务共享的 Redis 键**：切换窗口里 channel-server
// 和 lark-service 会同时在跑，键名不一致的后果不是出错，是两边各自去打一遍 meme 服务
// （请求量翻倍、缓存命中率减半）。所以键名逐字钉住。

import { describe, expect, it } from 'bun:test';

import type { LarkCommandCache } from '../rules/commands';
import { MEME_LIST_CACHE_KEY, MEME_LIST_CACHE_SECONDS, cachedMemeTemplates } from './memes';
import type { LarkMeme } from './memes';

function meme(key: string, keywords: string[]): LarkMeme {
    return { key, keywords, params_type: {} };
}

function memory(seed?: string): { cache: LarkCommandCache; wrote: [string, string, number][] } {
    const store = new Map<string, string>();
    if (seed !== undefined) store.set(MEME_LIST_CACHE_KEY, seed);
    const wrote: [string, string, number][] = [];
    return {
        wrote,
        cache: {
            get: async (key) => store.get(key) ?? null,
            setWithExpire: async (key, value, seconds) => {
                wrote.push([key, value, seconds]);
                store.set(key, value);
            },
        },
    };
}

describe('模板列表的缓存', () => {
    it('第一次打服务、把结果写进缓存，第二次直接读缓存', async () => {
        const { cache, wrote } = memory();
        let calls = 0;
        const templates = cachedMemeTemplates(cache, async () => {
            calls += 1;
            return [meme('petpet', ['摸'])];
        });

        expect(await templates()).toEqual([meme('petpet', ['摸'])]);
        expect(await templates()).toEqual([meme('petpet', ['摸'])]);
        expect(calls).toBe(1);
        expect(wrote).toEqual([
            [MEME_LIST_CACHE_KEY, JSON.stringify([meme('petpet', ['摸'])]), MEME_LIST_CACHE_SECONDS],
        ]);
    });

    // 键名和 TTL 都是拆分前那个 @cache 装饰器算出来的（`${方法名}:${参数的 JSON}`）。
    // 切换窗口里两个服务同时在跑，键名不一致 = 两边各打一遍 meme 服务。
    it('键名与 TTL 与拆分前逐字相同', () => {
        expect(MEME_LIST_CACHE_KEY).toBe('getMemeList:[]');
        expect(MEME_LIST_CACHE_SECONDS).toBe(600);
    });

    it('缓存里已经有就不打服务', async () => {
        const { cache } = memory(JSON.stringify([meme('cached', ['缓'])]));
        const templates = cachedMemeTemplates(cache, async () => {
            throw new Error('should not have been asked');
        });

        expect(await templates()).toEqual([meme('cached', ['缓'])]);
    });

    // Redis 挂了就抛，**不静默降级去打服务**：与拆分前那个装饰器一致（它对 get 的异常
    // 不做任何处理）。抛出去之后由指令层接住 —— checkMeme 当作"不是 meme"，genMeme 对着
    // 用户说一句。降级听起来更友好，但那是一个上游没有的行为。
    it('读缓存失败就抛', async () => {
        const templates = cachedMemeTemplates(
            {
                get: async () => {
                    throw new Error('redis is down');
                },
                setWithExpire: async () => {},
            },
            async () => [meme('petpet', ['摸'])],
        );

        expect(templates()).rejects.toThrow('redis is down');
    });

    // 上游这一格是"解析不了就把那串字原样返回"，于是调用方拿到一个 string 当数组用、
    // 在 `.find` 上炸出一个 TypeError。这里改成直接抛：可观测的落点完全一样（都是指令
    // 层那个 catch），但报错说得清是怎么回事。库里的值是我们自己写的，这条路走不到。
    it('缓存里存着坏 JSON 时抛，而不是把那串字当成列表', async () => {
        const { cache } = memory('not json');
        const templates = cachedMemeTemplates(cache, async () => [meme('petpet', ['摸'])]);

        expect(templates()).rejects.toThrow(/cache/i);
    });
});
