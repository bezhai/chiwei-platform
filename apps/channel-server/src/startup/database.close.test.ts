import { describe, it, expect } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resetRedisClient } from '@inner/shared/cache';

// 关停顺序是进程退出前唯一的收尾窗口：SIGTERM handler 调完 DatabaseManager.close()
// 就 process.exit，没有第二次机会。Redis 有两条连接（命令 + 订阅）要 quit，close()
// 必须真的等到它们断开，而不是把 Promise 丢在地上就打印"已关闭"。
//
// 这里刻意不用 mock.module 去桩 @inner/shared/cache 观察调用：database.ts 是
// **动态 import** 该模块，而 bun 的 mock.module 是进程级全局、后写覆盖先写——
// 同进程里任何一个后加载的测试文件重新桩了它（哪怕是 spread 真身），这里的桩就
// 被换掉，测试变成顺序依赖的假红/假绿。改用两条与执行序无关的断言。
describe('关停路径必须等待 Redis 连接关闭', () => {
    it('resetRedisClient 是 async——返回 Promise，调用方才有得可 await', () => {
        // 它曾经是 `(): void`，内部 `defaultInstance.close()` 的 Promise 直接丢弃：
        // 连接没断进程就退了，且 close() 抛错会成为 unhandled rejection、
        // 连 database.ts 外层的 try/catch 都捕获不到。
        expect(resetRedisClient.constructor.name).toBe('AsyncFunction');
    });

    it('DatabaseManager.close 每一处 resetRedisClient 调用都带 await', () => {
        const src = readFileSync(new URL('./database.ts', import.meta.url), 'utf8');

        const allCalls = src.match(/resetRedisClient\(\)/g) ?? [];
        const awaitedCalls = src.match(/await\s+resetRedisClient\(\)/g) ?? [];

        expect(allCalls.length).toBeGreaterThan(0);
        // 源码级断言是因为 eslint 在本仓库跑不起来（eslint.config.js 用 require 但
        // package.json 是 "type": "module"），no-floating-promises 这道防线不存在。
        // 等 lint 修好后这条可以退役。
        expect(awaitedCalls.length).toBe(allCalls.length);
    });
});
