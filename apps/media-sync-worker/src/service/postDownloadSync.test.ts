import { describe, expect, it, mock } from 'bun:test';
import { runPostDownloadSync, schedulePostDownloadSync } from './postDownloadSync';

describe('schedulePostDownloadSync', () => {
    it('returns immediately and logs after the background sync resolves', async () => {
        let resolveSync!: (value: { status: 'tagger_disabled' }) => void;
        const syncPromise = new Promise<{ status: 'tagger_disabled' }>((resolve) => {
            resolveSync = resolve;
        });
        const syncMinioAndMaybeSubmitTagger = mock(() => syncPromise);
        const logger = {
            info: mock((_message: string) => {}),
            warn: mock((_message: string, _error?: unknown) => {}),
        };
        const nowValues = [100, 130];

        schedulePostDownloadSync('a.jpg', {
            syncMinioAndMaybeSubmitTagger,
            logger,
            nowMs: () => nowValues.shift() ?? 130,
        });

        expect(syncMinioAndMaybeSubmitTagger).toHaveBeenCalledTimes(1);
        expect(logger.info).not.toHaveBeenCalled();

        resolveSync({ status: 'tagger_disabled' });
        await syncPromise;
        await Promise.resolve();

        expect(logger.info).toHaveBeenCalledTimes(1);
        const message = logger.info.mock.calls[0][0];
        expect(message).toContain('download_post_sync_timing');
        expect(message).toContain('"pixiv_addr":"a.jpg"');
        expect(message).toContain('"status":"tagger_disabled"');
        expect(message).toContain('"total_ms":30');
    });

    it('swallows background sync errors and logs a warning', async () => {
        const syncMinioAndMaybeSubmitTagger = mock(async () => {
            throw new Error('sync boom');
        });
        const logger = {
            info: mock((_message: string) => {}),
            warn: mock((_message: string, _error?: unknown) => {}),
        };

        schedulePostDownloadSync('a.jpg', {
            syncMinioAndMaybeSubmitTagger,
            logger,
            nowMs: () => 10,
        });
        await Promise.resolve();
        await Promise.resolve();

        expect(logger.warn).toHaveBeenCalledTimes(1);
        expect(logger.warn.mock.calls[0][0]).toContain('download_post_sync_failed');
        expect(logger.warn.mock.calls[0][0]).toContain('a.jpg');
    });

    it('adds mirror result metadata without changing the tagger result', async () => {
        const result = await runPostDownloadSync('a.jpg', {
            syncMinioAndMaybeSubmitTagger: mock(async () => ({ status: 'queued' as const })),
            syncPixivImageToLocal: mock(async () => ({ status: 'synced' as const, count: 2 })),
        });

        expect(result).toEqual({
            status: 'queued',
            pixiv_image_mirror_status: 'synced',
            pixiv_image_mirror_count: 2,
        });
    });

    it('does not fail the tagger path when mirror sync fails', async () => {
        const logger = {
            info: mock((_message: string) => {}),
            warn: mock((_message: string) => {}),
        };

        const result = await runPostDownloadSync('a.jpg', {
            syncMinioAndMaybeSubmitTagger: mock(async () => ({ status: 'tagger_disabled' as const })),
            syncPixivImageToLocal: mock(async () => {
                throw new Error('mongo down');
            }),
            logger,
        });

        expect(result).toEqual({
            status: 'tagger_disabled',
            pixiv_image_mirror_status: 'failed',
            pixiv_image_mirror_error: 'mongo down',
        });
        expect(logger.warn).toHaveBeenCalledTimes(1);
        expect(logger.warn.mock.calls[0][0]).toContain('pixiv_image_mirror_sync_failed');
    });
});
