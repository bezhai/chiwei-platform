import { describe, expect, it } from 'bun:test';
import { ObjectId } from 'mongodb';
import { buildPixivImageMirrorOperations } from './imageMirrorOperations';

describe('buildPixivImageMirrorOperations', () => {
    it('sets every dynamic source field without replacing local-owned enrichment', () => {
        const sourceId = new ObjectId('507f1f77bcf86cd799439011');
        const operations = buildPixivImageMirrorOperations([
            {
                _id: sourceId,
                pixiv_addr: 'a.jpg',
                tos_file_name: 'pixiv/2026/a.jpg',
                dynamic_source_field: { nested: true },
                image_key: 'source-stale-key',
                width: 1200,
                height: 1800,
                update_time: new Date('2026-01-01T00:00:00.000Z'),
                tagger_result: { stale: true },
            },
        ]);

        expect(operations).toEqual([
            {
                updateOne: {
                    filter: { _id: sourceId },
                    update: {
                        $set: {
                            pixiv_addr: 'a.jpg',
                            tos_file_name: 'pixiv/2026/a.jpg',
                            dynamic_source_field: { nested: true },
                        },
                        $setOnInsert: {
                            _id: sourceId,
                            image_key: 'source-stale-key',
                            width: 1200,
                            height: 1800,
                            update_time: new Date('2026-01-01T00:00:00.000Z'),
                            tagger_result: { stale: true },
                        },
                    },
                    upsert: true,
                },
            },
        ]);
    });

    it('does not whitelist or drop unknown source fields', () => {
        const sourceId = new ObjectId('507f1f77bcf86cd799439012');
        const [operation] = buildPixivImageMirrorOperations([
            { _id: sourceId, pixiv_addr: 'b.png', future_payload: ['kept'] },
        ]);

        expect((operation as any).updateOne.update.$set.future_payload).toEqual(['kept']);
    });

    it('never overwrites fields owned by local Lark upload or Tagger projection', () => {
        const [operation] = buildPixivImageMirrorOperations([{
            _id: new ObjectId('507f1f77bcf86cd799439013'),
            pixiv_addr: 'c.png',
            image_key: '',
            tagger_search_terms: ['stale'],
            tagger_task_id: 'old-task',
        }]);

        const update = (operation as any).updateOne.update;
        expect(update.$set.image_key).toBeUndefined();
        expect(update.$set.tagger_search_terms).toBeUndefined();
        expect(update.$set.tagger_task_id).toBeUndefined();
        expect(update.$setOnInsert).toMatchObject({
            image_key: '',
            tagger_search_terms: ['stale'],
            tagger_task_id: 'old-task',
        });
    });
});
