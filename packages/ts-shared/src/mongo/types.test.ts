import { describe, expect, it } from 'bun:test';
import { MongoClient } from 'mongodb';
import { buildMongoUrl } from './types';

describe('buildMongoUrl', () => {
    it('includes socketTimeoutMS in the connection URL when configured', () => {
        const url = buildMongoUrl({
            host: 'mongo-host',
            port: 27017,
            database: 'chiwei',
            authSource: 'admin',
            connectTimeoutMS: 10000,
            socketTimeoutMS: 60000,
        });

        expect(url).toContain('socketTimeoutMS=60000');
        expect(url).toContain('connectTimeoutMS=10000');
    });

    it('omits socketTimeoutMS when not configured or explicitly 0 (driver default applies)', () => {
        const url = buildMongoUrl({
            host: 'mongo-host',
            port: 27017,
            database: 'chiwei',
        });
        expect(url).not.toContain('socketTimeoutMS');

        const disabled = buildMongoUrl({
            host: 'mongo-host',
            port: 27017,
            database: 'chiwei',
            socketTimeoutMS: 0,
        });
        expect(disabled).not.toContain('socketTimeoutMS');
    });

    it('produces a URL the driver actually parses into socketTimeoutMS', () => {
        const url = buildMongoUrl({
            host: 'mongo-host',
            port: 27017,
            database: 'chiwei',
            socketTimeoutMS: 60000,
        });

        const client = new MongoClient(url);
        expect(client.options.socketTimeoutMS).toBe(60000);
    });
});
