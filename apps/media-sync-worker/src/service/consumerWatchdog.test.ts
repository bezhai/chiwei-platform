import { describe, expect, it } from 'bun:test';
import { ConsecutiveTimeoutGuard, CycleTimeoutError, runWithTimeout } from './consumerWatchdog';

describe('runWithTimeout', () => {
    it('passes through the resolved value when the cycle settles in time', async () => {
        await expect(runWithTimeout(Promise.resolve(42), 1000)).resolves.toBe(42);
    });

    it('passes through the cycle rejection unchanged', async () => {
        const boom = new Error('boom');

        await expect(runWithTimeout(Promise.reject(boom), 1000)).rejects.toBe(boom);
    });

    it('rejects with CycleTimeoutError when the cycle never settles', async () => {
        const neverSettles = new Promise<void>(() => {});

        await expect(runWithTimeout(neverSettles, 20)).rejects.toBeInstanceOf(CycleTimeoutError);
    });
});

describe('ConsecutiveTimeoutGuard', () => {
    it('fires the exhausted callback only at the consecutive-timeout threshold', () => {
        let exhausted = 0;
        const guard = new ConsecutiveTimeoutGuard(3, () => {
            exhausted++;
        });

        guard.recordTimeout();
        guard.recordTimeout();
        expect(exhausted).toBe(0);

        guard.recordTimeout();
        expect(exhausted).toBe(1);
    });

    it('a settled cycle resets the streak, so non-consecutive timeouts never exhaust', () => {
        let exhausted = 0;
        const guard = new ConsecutiveTimeoutGuard(3, () => {
            exhausted++;
        });

        guard.recordTimeout();
        guard.recordTimeout();
        guard.recordSettled();
        guard.recordTimeout();
        guard.recordTimeout();
        expect(exhausted).toBe(0);

        guard.recordTimeout();
        expect(exhausted).toBe(1);
    });
});
