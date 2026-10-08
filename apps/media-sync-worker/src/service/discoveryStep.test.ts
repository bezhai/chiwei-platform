import { describe, expect, it } from 'bun:test';
import { createDiscoveryStep, loadDiscoveryTimeoutMs } from './discoveryStep';

describe('discovery operation deadline', () => {
    it('bounds a stalled read and prevents late completion from enqueueing', async () => {
        let resolveRead!: (value: string[]) => void;
        const read = new Promise<string[]>((resolve) => { resolveRead = resolve; });
        const events: string[] = [];
        const step = createDiscoveryStep('author-1', 15, (message) => events.push(message));
        let enqueued = false;
        const discover = async () => {
            await step('redis.hget', () => read);
            enqueued = true;
        };
        await expect(discover()).rejects.toThrow('author=author-1 stage=redis.hget timed out after 15ms');
        resolveRead(['late']);
        await Bun.sleep(5);
        expect(enqueued).toBe(false);
        expect(events.some((entry) => entry.includes('status=failed'))).toBe(true);
    });

    it('returns success and propagates immediate errors without a later timeout', async () => {
        const events: string[] = [];
        const step = createDiscoveryStep('author-2', 15, (message) => events.push(message));
        expect(await step('read', async () => 42)).toBe(42);
        const error = new Error('unavailable');
        await expect(step('write', async () => { throw error; })).rejects.toBe(error);
        await Bun.sleep(25);
        expect(events.filter((entry) => entry.includes('status=failed'))).toHaveLength(1);
    });

    it('rejects invalid timeout configuration', () => {
        expect(loadDiscoveryTimeoutMs({})).toBe(180_000);
        expect(loadDiscoveryTimeoutMs({ DOWNLOAD_DISCOVERY_STEP_TIMEOUT_MS: '25' })).toBe(25);
        for (const value of ['0', '-1', 'NaN', '10ms', '1.5', 'Infinity']) {
            expect(() => loadDiscoveryTimeoutMs({ DOWNLOAD_DISCOVERY_STEP_TIMEOUT_MS: value })).toThrow();
        }
    });
});
