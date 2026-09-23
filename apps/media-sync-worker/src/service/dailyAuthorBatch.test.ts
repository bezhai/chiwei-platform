import { describe, expect, it } from 'bun:test';
import {
    assertDailyAuthorBatchSucceeded,
    runDailyAuthorBatch,
} from './dailyAuthorBatch';

const AUTHORS = [
    { userId: 'stuck', userName: 'stuck author' },
    { userId: 'healthy', userName: 'healthy author' },
];

describe('runDailyAuthorBatch', () => {
    it('times out a permanently pending author and continues with the next author', async () => {
        const started: string[] = [];
        const committed: string[] = [];
        const errors: string[] = [];
        let stuckSignal: AbortSignal | undefined;

        const summary = await runDailyAuthorBatch(AUTHORS, {
            authorTimeoutMs: 10,
            runAuthor: async (author, signal) => {
                started.push(author.userId);
                if (author.userId === 'stuck') {
                    stuckSignal = signal;
                    await new Promise<never>(() => {});
                }
            },
            afterAuthor: async (author) => {
                committed.push(author.userId);
            },
            logError: (message) => errors.push(message),
        });

        expect(started).toEqual(['stuck', 'healthy']);
        expect(committed).toEqual(['healthy']);
        expect(summary).toEqual({
            status: 'completed_with_errors',
            total: 2,
            completed: 1,
            failed: 0,
            timed_out: 1,
        });
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain('"author_id":"stuck"');
        expect(errors[0]).toContain('"status":"timed_out"');
        expect(errors[0]).toContain('"timeout_ms":10');
        expect(stuckSignal?.aborted).toBe(true);
    });

    it('isolates an ordinary author failure and reports accurate batch counts', async () => {
        const started: string[] = [];

        const summary = await runDailyAuthorBatch(AUTHORS, {
            authorTimeoutMs: 100,
            runAuthor: async (author) => {
                started.push(author.userId);
                if (author.userId === 'stuck') {
                    throw new Error('pixiv rejected request');
                }
            },
            logError: () => {},
        });

        expect(started).toEqual(['stuck', 'healthy']);
        expect(summary).toEqual({
            status: 'completed_with_errors',
            total: 2,
            completed: 1,
            failed: 1,
            timed_out: 0,
        });
    });

    it('isolates a post-watchdog cooldown commit failure and continues', async () => {
        const committed: string[] = [];

        const summary = await runDailyAuthorBatch(AUTHORS, {
            authorTimeoutMs: 100,
            runAuthor: async () => 'completed' as const,
            afterAuthor: async (author) => {
                committed.push(author.userId);
                if (author.userId === 'stuck') {
                    throw new Error('redis hset failed');
                }
            },
            logError: () => {},
        });

        expect(committed).toEqual(['stuck', 'healthy']);
        expect(summary).toEqual({
            status: 'completed_with_errors',
            total: 2,
            completed: 1,
            failed: 1,
            timed_out: 0,
        });
    });

    it('marks an all-settled batch as completed', async () => {
        const summary = await runDailyAuthorBatch(AUTHORS, {
            authorTimeoutMs: 100,
            runAuthor: async () => {},
            logError: () => {},
        });

        expect(summary).toEqual({
            status: 'completed',
            total: 2,
            completed: 2,
            failed: 0,
            timed_out: 0,
        });
    });

    it('turns a partial batch into a cron-visible failure after preserving its summary', () => {
        const summary = {
            status: 'completed_with_errors' as const,
            total: 2,
            completed: 1,
            failed: 1,
            timed_out: 0,
        };

        expect(() => assertDailyAuthorBatchSucceeded(summary)).toThrow(
            `daily author batch incomplete: ${JSON.stringify(summary)}`
        );
    });

    it('accepts a fully completed batch', () => {
        expect(() =>
            assertDailyAuthorBatchSucceeded({
                status: 'completed',
                total: 2,
                completed: 2,
                failed: 0,
                timed_out: 0,
            })
        ).not.toThrow();
    });
});
