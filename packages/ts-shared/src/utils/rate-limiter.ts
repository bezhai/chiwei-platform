import { Mutex } from 'async-mutex';

/**
 * 限速器看时间的方式。注入它只有一个目的：让"定时器早醒"这种时间边界能被确定性地
 * 演出来 —— 用真实 setTimeout 只能得到一个偶尔红的测试。
 */
export interface RateLimiterClock {
    now(): number;
    sleep(ms: number): Promise<void>;
}

const realClock: RateLimiterClock = {
    now: () => Date.now(),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
};

/**
 * 滑动窗口限速器：`interval` 毫秒内最多放行 `rate` 次。
 *
 * ## 醒来发现还差一点，要接着等而不是拒绝
 *
 * 等待时间按"最老的那次调用什么时候滑出窗口"算，但 `setTimeout` 不保证不早醒 ——
 * 定时器精度和系统负载都能让它偏一两毫秒。早醒之后窗口差一点点没滑过去，这时候直接
 * 返回 false 就等于把调用方剩下的等待预算全扔了：调用方说愿意等一秒，实际只等了五十
 * 毫秒就被拒。飞书发消息那两个限速器（每分钟 800 次、每秒 40 次）走的正是这条路。
 *
 * 所以这里是个循环：醒来重新判断，不够就在剩余预算内接着等，直到真拿到额度或者预算
 * 耗尽。判据始终是调用方给的那个 deadline，不是睡了几轮。
 */
export class RateLimiter {
    private rate: number;
    private interval: number;
    private queue: number[];
    private mutex: Mutex;
    private clock: RateLimiterClock;

    /**
     * @param rate 窗口内允许的最大次数
     * @param interval 窗口长度（毫秒）
     * @param clock 时间源，默认真实时钟
     */
    constructor(rate: number, interval: number, clock: RateLimiterClock = realClock) {
        this.rate = rate;
        this.interval = interval;
        this.queue = [];
        this.mutex = new Mutex();
        this.clock = clock;
    }

    private cleanup(now: number): void {
        while (this.queue.length > 0 && now - this.queue[0]! >= this.interval) {
            this.queue.shift();
        }
    }

    /**
     * 等一个放行额度。
     * @param timeout 最多愿意等多久（毫秒）
     * @returns 拿到额度返回 true；预算覆盖不了所需等待时间返回 false
     */
    public async waitForAllowance(timeout: number): Promise<boolean> {
        const deadline = this.clock.now() + timeout;

        for (;;) {
            // 等待期间不持锁：判额度是瞬时的，睡觉各睡各的。
            const verdict = await this.mutex.runExclusive(() => {
                const now = this.clock.now();
                this.cleanup(now);

                if (this.queue.length < this.rate) {
                    this.queue.push(now);
                    return { allowed: true, readyAt: now };
                }
                return { allowed: false, readyAt: this.queue[0]! + this.interval };
            });

            if (verdict.allowed) return true;
            // 明知等不到就不白等。
            if (verdict.readyAt > deadline) return false;

            // 至少睡 1ms：readyAt 已经过去时不能变成忙循环。
            await this.clock.sleep(Math.max(verdict.readyAt - this.clock.now(), 1));
        }
    }

    /**
     * 当前窗口内已占用的额度数
     */
    public getQueueSize(): number {
        return this.queue.length;
    }

    /**
     * 清空窗口
     */
    public reset(): void {
        this.queue = [];
    }
}
