import { describe, test, expect } from 'bun:test';
import { RateLimiter } from '@inner/shared';

/**
 * RateLimiter 使用 Date.now() 与 setTimeout，测试使用较短的时间窗口来避免长时间等待。
 */
describe('utils/rate-limiting/rate-limiter', () => {
    test('在空队列时立即允许', async () => {
        const limiter = new RateLimiter(2, 1000); // 1s 内允许 2 次
        const allowed = await limiter.waitForAllowance(0);
        expect(allowed).toBe(true);
    });

    test('超过速率时等待至窗口滑动后允许', async () => {
        const limiter = new RateLimiter(1, 50); // 50ms 内只允许 1 次
        const first = await limiter.waitForAllowance(1000);
        expect(first).toBe(true);

        // 第二次会被排队，需等待 ~50ms；timeout 设置足够长
        const second = await limiter.waitForAllowance(1000);
        expect(second).toBe(true);
    });

    test('等待时间超过 timeout 时返回 false', async () => {
        const limiter = new RateLimiter(1, 1000); // 1s 内只允许 1 次
        const first = await limiter.waitForAllowance(1000);
        expect(first).toBe(true);

        // 第二次需要等待 ~1000ms，但超时时间设置为 100ms
        const second = await limiter.waitForAllowance(100);
        expect(second).toBe(false);
    });
});
