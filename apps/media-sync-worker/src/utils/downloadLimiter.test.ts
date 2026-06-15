import { describe, expect, it } from 'bun:test';
import { limitConcurrency } from './downloadLimiter';

describe('limitConcurrency', () => {
    it('keeps at most the requested number of tasks running and returns results in task order', async () => {
        let active = 0;
        let maxActive = 0;
        const delays = [30, 5, 5, 5];

        const tasks = [0, 1, 2, 3].map((value) => async () => {
            active++;
            maxActive = Math.max(maxActive, active);
            await new Promise((resolve) => setTimeout(resolve, delays[value]));
            active--;
            return value;
        });

        const results = await limitConcurrency(2, tasks);

        expect(maxActive).toBeLessThanOrEqual(2);
        expect(results).toEqual([0, 1, 2, 3]);
    });
});
