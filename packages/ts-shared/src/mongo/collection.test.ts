import { describe, expect, it } from 'bun:test';
import { MongoCollection } from './collection';

describe('MongoCollection.updateMany', () => {
    it('returns the driver UpdateResult so callers can observe modifiedCount', async () => {
        const nativeResult = {
            acknowledged: true,
            matchedCount: 2,
            modifiedCount: 2,
            upsertedCount: 0,
            upsertedId: null,
        };
        const stub = {
            updateMany: async () => nativeResult,
        };
        const collection = new MongoCollection(stub as any);

        const result = await collection.updateMany({}, {});

        expect(result.modifiedCount).toBe(2);
    });
});
