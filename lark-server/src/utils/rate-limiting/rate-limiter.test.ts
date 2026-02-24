import { RateLimiter } from '@inner/shared';

jest.useFakeTimers();

/**
 * 因 RateLimiter 使用 Date.now() 与 setTimeout，测试中用 jest.useFakeTimers() 控制时间推进。
 */
describe('utils/rate-limiting/rate-limiter', () => {
    beforeEach(() => {
        jest.clearAllTimers();
        jest.setSystemTime(0);
    });

    test('在空队列时立即允许', async () => {
        const limiter = new RateLimiter(2, 1000); // 1s 内允许 2 次
        const allowed = await limiter.waitForAllowance(0);
        expect(allowed).toBe(true);
    });

    test('超过速率时等待至窗口滑动后允许', async () => {
        const limiter = new RateLimiter(1, 1000); // 1s 内只允许 1 次
        const first = await limiter.waitForAllowance(1000);
        expect(first).toBe(true);

        // 第二次会被排队，需等待 1000ms；实现使用 now - first > interval，因此推进 1001ms
        const secondPromise = limiter.waitForAllowance(1000);
        await jest.advanceTimersByTimeAsync(1001);
        await expect(secondPromise).resolves.toBe(true);
    });

    test('等待时间超过 timeout 时返回 false', async () => {
        const limiter = new RateLimiter(1, 1000);
        const first = await limiter.waitForAllowance(1000);
        expect(first).toBe(true);

        // 第二次需要等待 1000ms，但超时时间设置为 500ms
        const second = await limiter.waitForAllowance(500);
        expect(second).toBe(false);
    });
});
