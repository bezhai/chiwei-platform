import { describe, expect, it, mock } from 'bun:test';
import { schedulePostDownloadSync } from './postDownloadSync';

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
});
