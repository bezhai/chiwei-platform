import { describe, expect, it } from 'bun:test';
import { ObjectId } from 'mongodb';
import { DuplicateKeyError, MemoryCollection, MemoryRedis } from './memoryStores';

// 替身本身的语义要和真实 Mongo / Redis 一致，导入测试的结论才可信。
describe('MemoryCollection', () => {
    const a = new ObjectId('000000000000000000000001');
    const b = new ObjectId('000000000000000000000002');

    it('throws on query operators it does not model', async () => {
        const col = new MemoryCollection('c', [{ _id: a, n: 1 }]);
        await expect(col.find({ n: { $gte: 1 } }).toArray()).rejects.toThrow('query operator $gte');
        await expect(col.find({ $or: [{ n: 1 }] }).toArray()).rejects.toThrow('field path $or');
        await expect(col.find({}, { skip: 1 } as any).toArray()).rejects.toThrow('find option skip');
    });

    it('applies $gt with BSON type bracketing and sorts by BSON type order', async () => {
        const col = new MemoryCollection('c', [{ _id: b }, { _id: 'str' }, { _id: a }]);
        expect((await col.find({}, { sort: { _id: 1 } }).toArray()).map((d) => d._id)).toEqual(['str', a, b]);
        expect(await col.find({ _id: { $gt: 'str' } }).toArray()).toEqual([]);
        expect((await col.find({ _id: { $gt: a } }).toArray()).map((d) => d._id)).toEqual([b]);
    });

    it('upserts from filter equality plus $setOnInsert and $set, matches without touching $setOnInsert fields', async () => {
        const col = new MemoryCollection('c', [{ _id: a, k: 'x', keep: 1 }]);
        const counts = await col.bulkWrite([
            { updateOne: { filter: { k: 'x' }, update: { $set: { s: 2 }, $setOnInsert: { keep: 9 } }, upsert: true } },
            { updateOne: { filter: { k: 'y' }, update: { $setOnInsert: { _id: b, k: 'y', v: 3 } }, upsert: true } },
        ], { ordered: false });

        expect(counts).toMatchObject({ matchedCount: 1, modifiedCount: 1, upsertedCount: 1 });
        expect(col.snapshot()).toEqual([
            { _id: a, k: 'x', keep: 1, s: 2 },
            { _id: b, k: 'y', v: 3 },
        ]);
    });

    it('rejects conflicting $set/$setOnInsert paths, unknown update operators and replacements', async () => {
        const col = new MemoryCollection('c');
        await expect(col.bulkWrite([
            { updateOne: { filter: { _id: a }, update: { $set: { f: 1 }, $setOnInsert: { f: 2 } }, upsert: true } },
        ])).rejects.toThrow('conflict');
        await expect(col.bulkWrite([
            { updateOne: { filter: { _id: a }, update: { $inc: { f: 1 } }, upsert: true } },
        ])).rejects.toThrow('update operator $inc');
        await expect(col.bulkWrite([
            { updateOne: { filter: { _id: a }, update: { f: 1 }, upsert: true } },
        ])).rejects.toThrow('atomic operators');
        await expect(col.bulkWrite([{ insertOne: { document: { f: 1 } } }])).rejects.toThrow('bulk operation insertOne');
    });

    it('raises a duplicate key error when an upsert would reuse an existing _id', async () => {
        const col = new MemoryCollection('c', [{ _id: a, k: 'x' }]);
        await expect(col.bulkWrite([
            { updateOne: { filter: { k: 'y' }, update: { $setOnInsert: { _id: a, k: 'y' } }, upsert: true } },
        ], { ordered: false })).rejects.toBeInstanceOf(DuplicateKeyError);
    });

    it('drops undefined fields like a BSON round trip and returns copies', async () => {
        const col = new MemoryCollection('c');
        await col.insertOne({ _id: a, gone: undefined, nested: { n: 1 } });
        const [doc] = await col.find({}).toArray();
        expect(doc).toEqual({ _id: a, nested: { n: 1 } });
        doc!.nested.n = 2;
        expect(col.snapshot()[0]!.nested.n).toBe(1);
    });
});

describe('MemoryRedis', () => {
    it('answers WRONGTYPE when a key holds another type', async () => {
        const redis = new MemoryRedis({ sets: { s: ['a'] } });
        await expect(redis.hgetall('s')).rejects.toThrow('WRONGTYPE');
        expect(await redis.hgetall('missing')).toEqual({});
        expect(await redis.get('missing')).toBeNull();
    });
});
