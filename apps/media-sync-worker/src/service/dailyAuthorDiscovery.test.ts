import { describe, expect, it } from 'bun:test';
import {
    enqueueDownloadTasks,
    runDailyAuthorDiscovery,
} from './dailyAuthorDiscovery';

const AUTHOR = { userId: '62922469', userName: 'test author' };

describe('runDailyAuthorDiscovery', () => {
    it('finishes discovery without committing cooldown state inside the watchdog', async () => {
        const events: string[] = [];

        const result = await runDailyAuthorDiscovery(
            AUTHOR,
            {
                getLastDownloadTime: async () => {
                    events.push('read_cooldown');
                    return null;
                },
                discoverAuthor: async () => {
                    events.push('discover');
                },
                waitAfterAuthor: async () => {
                    events.push('wait');
                },
                getRandomDays: () => 2,
                now: () => 1_700_000_000_000,
            },
            new AbortController().signal
        );

        expect(result).toBe('completed');
        expect(events).toEqual(['read_cooldown', 'discover', 'wait']);
    });

    it('preserves the discovery error', async () => {
        const failure = new Error('mongo insert failed');

        const promise = runDailyAuthorDiscovery(
            AUTHOR,
            {
                getLastDownloadTime: async () => null,
                discoverAuthor: async () => {
                    throw failure;
                },
                waitAfterAuthor: async () => {},
                getRandomDays: () => 2,
                now: () => 1_700_000_000_000,
            },
            new AbortController().signal
        );

        expect(promise).rejects.toBe(failure);
        await promise.catch(() => {});
    });

    it('rejects when the watchdog aborts late work', async () => {
        const controller = new AbortController();
        const timeout = new Error('author timed out');

        const promise = runDailyAuthorDiscovery(
            AUTHOR,
            {
                getLastDownloadTime: async () => null,
                discoverAuthor: async () => {
                    controller.abort(timeout);
                },
                waitAfterAuthor: async () => {},
                getRandomDays: () => 2,
                now: () => 1_700_000_000_000,
            },
            controller.signal
        );

        expect(promise).rejects.toBe(timeout);
        await promise.catch(() => {});
    });

    it('keeps an author inside its cooldown without rediscovering or rewriting it', async () => {
        let discoveries = 0;

        const result = await runDailyAuthorDiscovery(
            AUTHOR,
            {
                getLastDownloadTime: async () => '1700000000',
                discoverAuthor: async () => {
                    discoveries++;
                },
                waitAfterAuthor: async () => {},
                getRandomDays: () => 2,
                now: () => 1_700_000_100_000,
            },
            new AbortController().signal
        );

        expect(result).toBe('skipped');
        expect(discoveries).toBe(0);
    });
});

describe('enqueueDownloadTasks', () => {
    it('stops and preserves the original insertion error', async () => {
        const failure = new Error('write concern timed out');
        const inserted: string[] = [];

        const promise = enqueueDownloadTasks(
            ['101', '102', '103'],
            async (illustId) => {
                inserted.push(illustId);
                if (illustId === '102') {
                    throw failure;
                }
                return true;
            },
            new AbortController().signal
        );

        expect(promise).rejects.toBe(failure);
        await promise.catch(() => {});
        expect(inserted).toEqual(['101', '102']);
    });

    it('does not start another insertion after an in-flight write settles late', async () => {
        const controller = new AbortController();
        const timeout = new Error('author timed out');
        const inserted: string[] = [];

        const promise = enqueueDownloadTasks(
            ['101', '102'],
            async (illustId) => {
                inserted.push(illustId);
                controller.abort(timeout);
                return true;
            },
            controller.signal
        );

        expect(promise).rejects.toBe(timeout);
        await promise.catch(() => {});
        expect(inserted).toEqual(['101']);
    });
});
