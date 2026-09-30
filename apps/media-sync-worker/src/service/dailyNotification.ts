export class DailyNotificationTimeoutError extends Error {
    constructor(readonly timeoutMs: number) {
        super(`daily download notification did not settle within ${timeoutMs}ms`);
        this.name = 'DailyNotificationTimeoutError';
    }
}

export async function sendDailyNotificationWithTimeout(
    send: () => Promise<void>,
    timeoutMs: number
): Promise<void> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
        await Promise.race([
            Promise.resolve().then(send),
            new Promise<never>((_, reject) => {
                timer = setTimeout(
                    () => reject(new DailyNotificationTimeoutError(timeoutMs)),
                    timeoutMs
                );
            }),
        ]);
    } finally {
        if (timer !== undefined) {
            clearTimeout(timer);
        }
    }
}
