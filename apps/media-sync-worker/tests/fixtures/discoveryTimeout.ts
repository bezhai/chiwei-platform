import { mock } from 'bun:test';
import assert from 'node:assert/strict';

// Run in a separate process so module mocks cannot affect the worker test suite.
const scenario = process.argv[2];
process.env.DOWNLOAD_DISCOVERY_STEP_TIMEOUT_MS = '20';
process.env.DOWNLOAD_AFTER_AUTHOR_DELAY_MS = '0';
const never = () => new Promise<never>(() => {});
const enqueued: string[] = [];
const successfulAuthors: string[] = [];
const notifications: string[] = [];
let resolveLate!: (value: string[]) => void;
const late = new Promise<string[]>((resolve) => { resolveLate = resolve; });

mock.module('../../src/pixiv/settings', () => ({
    getBanIllusts: async () => {
        if (scenario === 'ban_config') throw new Error('Dynamic Config pixiv_ban_illusts is missing');
        return [];
    },
}));
mock.module('../../src/pixiv/pixiv', () => ({
    getFollowersByTag: async () => [{ userId: '1', userName: 'one' }, { userId: '2', userName: 'two' }],
    getAuthorArtwork: async (id: string) => id === '1' && scenario === 'proxy' ? late : [`${id}01`, '100'],
    getTagArtwork: async () => [],
}));
// Keep the real repository code, including find-before-insert, in this test.
const collections = {
    images: { find: async () => [{ illust_id: 100 }] },
    tagTranslations: {},
    authorDownloads: {
        findOne: async (filter: { _id: string }) => {
            if (filter._id === '1' && scenario === 'throttle') return never();
            if (filter._id === '1' && scenario === 'notification') throw new Error('read failed');
            return null;
        },
        updateOne: async (filter: { _id: string }) => { successfulAuthors.push(filter._id); },
    },
    downloadTasks: {
        find: async (filter: { illust_id: string }) => filter.illust_id === '101' && scenario === 'dedup' ? late : [],
        insertOne: async (task: { illust_id: string }) => {
            if (task.illust_id === '101' && scenario === 'enqueue') throw new Error('insert failed');
            enqueued.push(task.illust_id);
        },
    },
};
mock.module('../../src/mongo/client', () => ({
    pixivCollections: async () => collections,
}));
mock.module('../../src/lark', () => ({
    send_msg: async (_chat: string, message: string) => {
        notifications.push(message);
        if (scenario === 'notification' && message.includes('图片下载失败')) return never();
    },
}));
const { startDownload } = await import('../../src/service/dailyDownload');

if (scenario === 'ban_config') {
    // An unreadable ban list must stop the run, never be read as "nothing is banned".
    await assert.rejects(startDownload(), /pixiv_ban_illusts/);
    assert.deepEqual(enqueued, [], 'no author may enqueue without the ban list');
    assert.deepEqual(successfulAuthors, [], 'no author may record success without the ban list');
    assert.ok(notifications.includes('下载图片服务获取元信息失败'), 'the run failure must be reported');
} else {
    await startDownload();
    resolveLate(scenario === 'dedup' ? [] : ['101', '100']);
    await Bun.sleep(30);
    assert.deepEqual(successfulAuthors, ['2'], 'failed/timed-out author must not record success');
    assert.deepEqual(enqueued, ['201'], 'next author must enqueue; late first author must not enqueue');
}
console.log('DISCOVERY_SCENARIO_OK');
