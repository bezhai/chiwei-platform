import { describe, expect, it } from 'bun:test';
import { projectTaggerResultToCollection } from './taggerProjection';

describe('projectTaggerResultToCollection', () => {
    it('updates every duplicate local document while fencing older generations', async () => {
        const calls: Array<Record<string, unknown>> = [];
        const collection = {
            async updateMany(filter: Record<string, unknown>, update: Record<string, unknown>) {
                calls.push({ filter, update });
                return { matchedCount: 2 };
            },
        };
        const row = { id: 'a.jpg', schema_version: 1, future: { value: 'search me' } };
        const projectedAt = new Date('2026-07-22T08:00:00.000Z');

        const count = await projectTaggerResultToCollection(collection, {
            pixivAddr: 'a.jpg',
            taskId: 'task-2',
            generation: 3,
            status: 'completed',
            result: row,
        }, projectedAt);

        expect(count).toBe(2);
        expect(calls).toEqual([{
            filter: {
                pixiv_addr: 'a.jpg',
                $or: [
                    { tagger_generation: { $exists: false } },
                    { tagger_generation: { $lt: 3 } },
                    {
                        tagger_generation: 3,
                        $or: [
                            { tagger_task_id: { $exists: false } },
                            { tagger_task_id: 'task-2' },
                        ],
                    },
                ],
            },
            update: {
                $set: {
                    tagger_result: row,
                    tagger_search_terms: ['a.jpg', 'search me'],
                    tagger_task_id: 'task-2',
                    tagger_generation: 3,
                    tagger_status: 'completed',
                    tagger_updated_at: projectedAt,
                },
            },
        }]);
    });
});
