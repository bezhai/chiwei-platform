import { afterEach, beforeEach, describe, expect, it, mock } from 'bun:test';
import type { PixivImageInfo } from '../mongo/types';
import {
    bestEffortSyncToMinio,
    syncPixivToMinioForTagger,
    type BestEffortSyncDeps,
} from './syncPage';

// Dependencies are injected (not mock.module) so this test can't pollute the
// real ./syncToMinio module for sibling test files in the same bun run.
let findImpl: (pixivAddr: string) => Promise<PixivImageInfo | null> = async () => null;
let syncImpl: (key: string) => Promise<void> = async () => {};

const findImageByPixivAddr = mock((pixivAddr: string) => findImpl(pixivAddr));
const syncOssObjectToMinio = mock((key: string) => syncImpl(key));

const deps: BestEffortSyncDeps = {
    findImageByPixivAddr,
    syncOssObjectToMinio,
};

function resetMocks() {
    findImageByPixivAddr.mockClear();
    syncOssObjectToMinio.mockClear();
    // default: a doc with a non-empty tos_file_name, sync succeeds
    findImpl = async () => ({
        visible: true,
        pixiv_addr: '123_p0.png',
        tos_file_name: 'pixiv_img_v2/20260604/123_p0.png',
    });
    syncImpl = async () => {};
}

const ENV_KEY = 'MINIO_SYNC_ENABLED';

// env.d.ts declares MINIO_SYNC_ENABLED as a (non-optional) string for prod, so
// `delete process.env.MINIO_SYNC_ENABLED` is a type error. Tests still need to
// simulate "unset", so go through a loosely-typed view of process.env here.
const mutableEnv = process.env as Record<string, string | undefined>;

function setEnv(value: string | undefined): void {
    if (value === undefined) {
        delete mutableEnv[ENV_KEY];
    } else {
        mutableEnv[ENV_KEY] = value;
    }
}

describe('bestEffortSyncToMinio', () => {
    // The whole feature sits behind MINIO_SYNC_ENABLED (default OFF). The cases
    // below assert "what happens once the switch is ON", so flip it ON per-test
    // and restore the original env afterwards to avoid leaking into siblings.
    let originalEnv: string | undefined;

    beforeEach(() => {
        resetMocks();
        originalEnv = process.env[ENV_KEY];
        setEnv('true');
    });

    afterEach(() => {
        setEnv(originalEnv);
    });

    it('happy path: found non-empty tos_file_name -> calls syncOssObjectToMinio once with that key', async () => {
        findImpl = async () => ({
            visible: true,
            pixiv_addr: '123_p0.png',
            tos_file_name: 'pixiv_img_v2/20260604/123_p0.png',
        });

        await bestEffortSyncToMinio('123_p0.png', deps);

        expect(findImageByPixivAddr).toHaveBeenCalledTimes(1);
        expect(findImageByPixivAddr.mock.calls[0][0]).toBe('123_p0.png');

        expect(syncOssObjectToMinio).toHaveBeenCalledTimes(1);
        expect(syncOssObjectToMinio.mock.calls[0][0]).toBe('pixiv_img_v2/20260604/123_p0.png');
    });

    it('best-effort: syncOssObjectToMinio throws -> does NOT reject, swallows error with a log', async () => {
        const warnSpy = mock((..._args: any[]) => {});
        const originalWarn = console.warn;
        console.warn = warnSpy as any;

        syncImpl = async () => {
            throw new Error('MinIO put boom');
        };

        try {
            // must NOT reject
            await bestEffortSyncToMinio('123_p0.png', deps);
        } finally {
            console.warn = originalWarn;
        }

        // it did attempt the sync
        expect(syncOssObjectToMinio).toHaveBeenCalledTimes(1);
        // the error was swallowed and logged (with the pixivAddr for locating misses)
        expect(warnSpy).toHaveBeenCalled();
        const logged = warnSpy.mock.calls.map((c) => c.join(' ')).join(' ');
        expect(logged).toContain('123_p0.png');
    });

    it('no tos_file_name (doc missing) -> does NOT call syncOssObjectToMinio, does NOT throw', async () => {
        findImpl = async () => null;

        await bestEffortSyncToMinio('123_p0.png', deps);

        expect(findImageByPixivAddr).toHaveBeenCalledTimes(1);
        expect(syncOssObjectToMinio).not.toHaveBeenCalled();
    });

    it('empty tos_file_name -> does NOT call syncOssObjectToMinio, does NOT throw', async () => {
        findImpl = async () => ({
            visible: true,
            pixiv_addr: '123_p0.png',
            tos_file_name: '',
        });

        await bestEffortSyncToMinio('123_p0.png', deps);

        expect(syncOssObjectToMinio).not.toHaveBeenCalled();
    });

    it('best-effort: findImageByPixivAddr throws -> does NOT reject, does NOT call sync', async () => {
        findImpl = async () => {
            throw new Error('mongo boom');
        };

        await bestEffortSyncToMinio('123_p0.png', deps);

        expect(syncOssObjectToMinio).not.toHaveBeenCalled();
    });

    it('time-bounded: syncOssObjectToMinio hangs forever -> resolves after timeout, logs with pixivAddr, does NOT hang/reject', async () => {
        const warnSpy = mock((..._args: any[]) => {});
        const originalWarn = console.warn;
        console.warn = warnSpy as any;

        // never resolves -> would hold the concurrency slot forever without a timeout
        syncImpl = () => new Promise<void>(() => {});

        const start = Date.now();
        try {
            await bestEffortSyncToMinio('123_p0.png', {
                ...deps,
                timeoutMs: 20,
            });
        } finally {
            console.warn = originalWarn;
        }
        const elapsed = Date.now() - start;

        // it attempted the sync but bailed out via timeout, not via the hung promise
        expect(syncOssObjectToMinio).toHaveBeenCalledTimes(1);
        expect(elapsed).toBeLessThan(2000);

        // the timeout was logged with the pixivAddr for locating misses
        expect(warnSpy).toHaveBeenCalled();
        const logged = warnSpy.mock.calls.map((c) => c.join(' ')).join(' ');
        expect(logged).toContain('123_p0.png');
    });

    it('switch ON via "1": runs the original flow (found key -> calls sync once)', async () => {
        setEnv('1');

        await bestEffortSyncToMinio('123_p0.png', deps);

        expect(findImageByPixivAddr).toHaveBeenCalledTimes(1);
        expect(syncOssObjectToMinio).toHaveBeenCalledTimes(1);
        expect(syncOssObjectToMinio.mock.calls[0][0]).toBe('pixiv_img_v2/20260604/123_p0.png');
    });
});

describe('bestEffortSyncToMinio: MINIO_SYNC_ENABLED switch OFF (default)', () => {
    let originalEnv: string | undefined;

    beforeEach(() => {
        resetMocks();
        originalEnv = process.env[ENV_KEY];
    });

    afterEach(() => {
        setEnv(originalEnv);
    });

    const offCases: Array<[string, string | undefined]> = [
        ['unset', undefined],
        ['"false"', 'false'],
        ['"0"', '0'],
        ['"FALSE"', 'FALSE'],
        ['arbitrary string', 'yes'],
    ];

    for (const [label, value] of offCases) {
        it(`${label} -> immediate no-op: no Mongo lookup, no sync, no throw`, async () => {
            setEnv(value);

            await bestEffortSyncToMinio('123_p0.png', deps);

            // OFF means full no-op: never touches Mongo / OSS / MinIO.
            expect(findImageByPixivAddr).not.toHaveBeenCalled();
            expect(syncOssObjectToMinio).not.toHaveBeenCalled();
        });
    }
});

describe('syncPixivToMinioForTagger', () => {
    let originalEnv: string | undefined;

    beforeEach(() => {
        resetMocks();
        originalEnv = process.env[ENV_KEY];
        setEnv('true');
    });

    afterEach(() => {
        setEnv(originalEnv);
    });

    it('returns synced with basename objectName after OSS -> MinIO sync succeeds', async () => {
        const result = await syncPixivToMinioForTagger('123_p0.png', deps);

        expect(result).toEqual({
            status: 'synced',
            pixivAddr: '123_p0.png',
            ossKey: 'pixiv_img_v2/20260604/123_p0.png',
            objectName: '123_p0.png',
        });
        expect(syncOssObjectToMinio).toHaveBeenCalledTimes(1);
    });

    it('returns disabled without touching Mongo when MINIO_SYNC_ENABLED is off', async () => {
        setEnv(undefined);

        const result = await syncPixivToMinioForTagger('123_p0.png', deps);

        expect(result).toEqual({
            status: 'disabled',
            pixivAddr: '123_p0.png',
        });
        expect(findImageByPixivAddr).not.toHaveBeenCalled();
        expect(syncOssObjectToMinio).not.toHaveBeenCalled();
    });

    it('returns missing_key when the source doc has no tos_file_name', async () => {
        findImpl = async () => null;

        const result = await syncPixivToMinioForTagger('123_p0.png', deps);

        expect(result).toEqual({
            status: 'missing_key',
            pixivAddr: '123_p0.png',
        });
        expect(syncOssObjectToMinio).not.toHaveBeenCalled();
    });

    it('returns failed instead of throwing when sync fails', async () => {
        syncImpl = async () => {
            throw new Error('MinIO put boom');
        };

        const result = await syncPixivToMinioForTagger('123_p0.png', deps);

        expect(result.status).toBe('failed');
        expect(result.pixivAddr).toBe('123_p0.png');
        if (result.status !== 'failed') {
            throw new Error('expected failed result');
        }
        expect(result.error).toContain('MinIO put boom');
    });

    it('returns timeout when sync does not finish before timeoutMs', async () => {
        syncImpl = () => new Promise<void>(() => {});

        const result = await syncPixivToMinioForTagger('123_p0.png', {
            ...deps,
            timeoutMs: 20,
        });

        expect(result).toEqual({
            status: 'timeout',
            pixivAddr: '123_p0.png',
            ossKey: 'pixiv_img_v2/20260604/123_p0.png',
            objectName: '123_p0.png',
            timeoutMs: 20,
        });
    });
});
