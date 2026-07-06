/**
 * consumer 循环 watchdog：单轮硬时间上限 + 连续超时自愈。
 *
 * watchdog 不能释放底层挂死的 promise——不承诺资源回收，只承诺消费不永久停摆：
 * 单次超时 continue（可能是极端慢任务或孤立挂点，不误伤同进程的 tagger worker / cron），
 * 连续超时达到阈值说明系统性故障（如连接池被泄漏协程耗尽），退出进程交给 K8s 重拉。
 */

export class CycleTimeoutError extends Error {
    constructor(timeoutMs: number) {
        super(`consumer cycle did not settle within ${timeoutMs}ms watchdog limit`);
        this.name = 'CycleTimeoutError';
    }
}

/**
 * 给单轮循环包一层硬超时：cycle 在限时内 settle 则透传结果 / 原样抛错；
 * 超时则抛 CycleTimeoutError（cycle 本身仍在后台继续，其迟到收尾由领取代次条件化丢弃）。
 */
export async function runWithTimeout<T>(cycle: Promise<T>, timeoutMs: number): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
        return await Promise.race([
            cycle,
            new Promise<never>((_, reject) => {
                timer = setTimeout(() => reject(new CycleTimeoutError(timeoutMs)), timeoutMs);
            }),
        ]);
    } finally {
        if (timer !== undefined) {
            clearTimeout(timer);
        }
    }
}

/**
 * 连续超时计数器：recordTimeout 累加、达到阈值触发 onExhausted（生产为 process.exit，
 * 以回调注入以便测试不真杀进程）；recordSettled（正常完成或普通错误的轮次）清零。
 */
export class ConsecutiveTimeoutGuard {
    private consecutiveTimeouts = 0;

    constructor(
        private readonly threshold: number,
        private readonly onExhausted: () => void
    ) {}

    recordTimeout(): void {
        this.consecutiveTimeouts++;
        if (this.consecutiveTimeouts >= this.threshold) {
            this.onExhausted();
        }
    }

    recordSettled(): void {
        this.consecutiveTimeouts = 0;
    }
}
