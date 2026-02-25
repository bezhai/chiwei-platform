/**
 * Bun test preload: mock modules that cause compatibility issues at import time.
 *
 * @inner/shared barrel-exports from './mongo' which imports 'mongodb' at the top level.
 * MongoDB v6 has ESM compatibility issues with Bun 1.x, causing:
 *   SyntaxError: export 'BulkWriteResult' not found in './types'
 *
 * We mock the mongodb package before any module loads it.
 */
import { mock } from 'bun:test';

mock.module('mongodb', () => ({
    MongoClient: class MockMongoClient {
        connect() {
            return Promise.resolve(this);
        }
        db() {
            return {
                collection: () => ({
                    insertOne: () => Promise.resolve({ insertedId: 'mock' }),
                    insertMany: () => Promise.resolve({ insertedIds: {} }),
                    findOne: () => Promise.resolve(null),
                    find: () => ({ toArray: () => Promise.resolve([]) }),
                    updateOne: () => Promise.resolve({ modifiedCount: 0 }),
                    deleteOne: () => Promise.resolve({ deletedCount: 0 }),
                    createIndex: () => Promise.resolve('mock_index'),
                    bulkWrite: () =>
                        Promise.resolve({
                            insertedCount: 0,
                            matchedCount: 0,
                            modifiedCount: 0,
                            deletedCount: 0,
                            upsertedCount: 0,
                        }),
                }),
            };
        }
        close() {
            return Promise.resolve();
        }
    },
    Collection: class {},
    Db: class {},
    ObjectId: class {
        toString() {
            return 'mock-object-id';
        }
    },
    Document: {},
    Filter: {},
    BulkWriteResult: class {},
}));
