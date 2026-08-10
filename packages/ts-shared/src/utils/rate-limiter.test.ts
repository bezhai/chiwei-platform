// 限速器的时间边界行为。
//
// 这里不用真实时钟：要钉的恰恰是"定时器早醒了一丁点"这种情况，而真实 setTimeout
// 什么时候早醒是不可控的 —— 用真钟只能得到一个偶尔红的测试，那和没测差不多
// （`apps/channel-server` 那份真钟测试就是这么偶发失败的，根因就在这里）。

import { describe, test, expect } from 'bun:test';

import { RateLimiter, type RateLimiterClock } from './rate-limiter';

/**
 * 手动推进的时钟。`sleep(ms)` 默认精确推进 ms，`drift` 可以让它少推一点，用来
 * 演"定时器比预定时间早醒"。
 */
function fakeClock(drift = 0): RateLimiterClock & { at: number; slept: number[] } {
    return {
        at: 1_000_000,
        slept: [],
        now() {
            return this.at;
        },
        async sleep(ms: number) {
            this.slept.push(ms);
            // 至少推进 1ms：真实的 sleep 不可能一点时间都不走，允许推进 0 就成了
            // "时间静止"，那是个演不出来的场景。
            this.at += Math.max(ms - drift, 1);
        },
    };
}

describe('RateLimiter', () => {
    test('额度没用完就直接放行', async () => {
        const clock = fakeClock();
        const limiter = new RateLimiter(2, 1000, clock);

        expect(await limiter.waitForAllowance(0)).toBe(true);
        expect(await limiter.waitForAllowance(0)).toBe(true);
        expect(clock.slept).toEqual([]);
    });

    test('额度用完就等到窗口滑过去，然后放行', async () => {
        const clock = fakeClock();
        const limiter = new RateLimiter(1, 50, clock);

        expect(await limiter.waitForAllowance(1000)).toBe(true);
        expect(await limiter.waitForAllowance(1000)).toBe(true);
        expect(clock.slept).toEqual([50]);
    });

    test('定时器早醒 1ms 不该让调用方吃到拒绝 —— 预算还剩 950ms', async () => {
        // 这是那个偶发失败的根因：醒来发现窗口差一点点没滑过去，旧实现直接返回
        // false，把调用方剩下的等待预算全扔了。正确的做法是接着等。
        const clock = fakeClock(1);
        const limiter = new RateLimiter(1, 50, clock);

        expect(await limiter.waitForAllowance(1000)).toBe(true);
        expect(await limiter.waitForAllowance(1000)).toBe(true);
        // 第一次等 50 差 1ms，补等 1ms 才够。
        expect(clock.slept).toEqual([50, 1]);
    });

    test('预算不够覆盖等待时间就立刻拒绝，不白等', async () => {
        const clock = fakeClock();
        const limiter = new RateLimiter(1, 1000, clock);

        expect(await limiter.waitForAllowance(1000)).toBe(true);
        expect(await limiter.waitForAllowance(100)).toBe(false);
        // 明知等不到就不该睡。
        expect(clock.slept).toEqual([]);
    });

    test('每次都早醒就每次都补等，轮数有限、最后一定拿到额度', async () => {
        // 早醒 10ms，于是补等的那几轮又会各自早醒。收敛靠的是时间总在往前走。
        const clock = fakeClock(10);
        const limiter = new RateLimiter(1, 50, clock);

        expect(await limiter.waitForAllowance(1000)).toBe(true);
        expect(await limiter.waitForAllowance(1000)).toBe(true);
        expect(clock.slept.length).toBeGreaterThan(1);
        // 补等的总时长仍在预算内 —— 早醒不该把等待放大到失控。
        expect(clock.at - 1_000_000).toBeLessThanOrEqual(1000);
    });

    test('放行会占掉额度，getQueueSize 看得见，reset 能清掉', async () => {
        const clock = fakeClock();
        const limiter = new RateLimiter(2, 1000, clock);

        await limiter.waitForAllowance(0);
        await limiter.waitForAllowance(0);
        expect(limiter.getQueueSize()).toBe(2);

        limiter.reset();
        expect(limiter.getQueueSize()).toBe(0);
        expect(await limiter.waitForAllowance(0)).toBe(true);
    });
});
