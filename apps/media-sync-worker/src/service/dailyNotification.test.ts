import { describe, expect, it } from 'bun:test';
import {
    DailyNotificationTimeoutError,
    sendDailyNotificationWithTimeout,
} from './dailyNotification';

describe('sendDailyNotificationWithTimeout', () => {
    it('settles when the notification completes', async () => {
        let sent = false;

        await sendDailyNotificationWithTimeout(async () => {
            sent = true;
        }, 100);

        expect(sent).toBe(true);
    });

    it('rejects a permanently pending notification within the configured bound', async () => {
        const promise = sendDailyNotificationWithTimeout(
            async () => new Promise<never>(() => {}),
            10
        );

        expect(promise).rejects.toBeInstanceOf(DailyNotificationTimeoutError);
        await promise.catch(() => {});
    });
});
