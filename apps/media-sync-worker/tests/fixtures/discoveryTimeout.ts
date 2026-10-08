import { mock } from 'bun:test';
import assert from 'node:assert/strict';

// Run in a separate process so module mocks cannot affect the worker test suite.
const scenario = process.argv[2];
process.env.DOWNLOAD_DISCOVERY_STEP_TIMEOUT_MS = '20';
process.env.DOWNLOAD_AFTER_AUTHOR_DELAY_MS = '0';
const never = () => new Promise<never>(() => {});
const enqueued: string[] = [];
const successfulAuthors: string[] = [];
let resolveLate!: (value: string[]) => void;
const late = new Promise<string[]>((resolve) => { resolveLate = resolve; });

mock.module('../../src/redis/redisClient', () => ({ default: {
    hget: async (_key: string, id: string) => {
        if (id === '1' && scenario === 'redis') return never();
        if (id === '1' && scenario === 'notification') throw new Error('read failed');
        return null;
    },
    hset: async (_key: string, id: string) => { successfulAuthors.push(id); return 1; },
    smembers: async () => [],
} }));
mock.module('../../src/pixiv/pixiv', () => ({
    getFollowersByTag: async () => [{ userId: '1', userName: 'one' }, { userId: '2', userName: 'two' }],
    getAuthorArtwork: async (id: string) => id === '1' && scenario === 'proxy' ? late : [`${id}01`, '100'],
    getTagArtwork: async () => [],
}));
// Keep the real repository code, including find-before-insert, in this test.
mock.module('../../src/mongo/client', () => ({
    ImgCollection: { find: async () => [{ illust_id: 100 }] },
    TranslateWordMap: {},
    DownloadTaskMap: {
        find: async (filter: { illust_id: string }) => filter.illust_id === '101' && scenario === 'dedup' ? late : [],
        insertOne: async (task: { illust_id: string }) => {
            if (task.illust_id === '101' && scenario === 'enqueue') throw new Error('insert failed');
            enqueued.push(task.illust_id);
        },
    },
}));
mock.module('../../src/lark', () => ({
    send_msg: async (_chat: string, message: string) => {
        if (scenario === 'notification' && message.includes('图片下载失败')) return never();
    },
}));
const { startDownload } = await import('../../src/service/dailyDownload');
await startDownload();
resolveLate(scenario === 'dedup' ? [] : ['101', '100']);
await Bun.sleep(30);
assert.deepEqual(successfulAuthors, ['2'], 'failed/timed-out author must not record success');
assert.deepEqual(enqueued, ['201'], 'next author must enqueue; late first author must not enqueue');
console.log('DISCOVERY_SCENARIO_OK');
