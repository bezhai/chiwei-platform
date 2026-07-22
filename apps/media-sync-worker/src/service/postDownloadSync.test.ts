import { describe, expect, it, mock } from 'bun:test';
import { runPostDownloadSync } from './postDownloadSync';

describe('runPostDownloadSync', () => {
    it('mirrors the source before ensuring the durable tagger outbox', async () => {
        const calls: string[] = [];

        const result = await runPostDownloadSync('a.jpg', {
            syncPixivImageToLocal: mock(async () => {
                calls.push('mirror');
                return { status: 'synced' as const, count: 2 };
            }),
            syncMinioAndMaybeSubmitTagger: mock(async () => {
                calls.push('outbox');
                return { status: 'queued' as const };
            }),
        });

        expect(calls).toEqual(['mirror', 'outbox']);
        expect(result).toEqual({
            status: 'queued',
            pixiv_image_mirror_status: 'synced',
            pixiv_image_mirror_count: 2,
        });
    });

    it('rejects without enqueueing when the source image is missing', async () => {
        const enqueue = mock(async () => ({ status: 'queued' as const }));

        await expect(
            runPostDownloadSync('a.jpg', {
                syncPixivImageToLocal: mock(async () => ({ status: 'missing_source' as const })),
                syncMinioAndMaybeSubmitTagger: enqueue,
            }),
        ).rejects.toThrow('missing source');

        expect(enqueue).not.toHaveBeenCalled();
    });

    it('propagates mirror failures and never starts the outbox handoff', async () => {
        const enqueue = mock(async () => ({ status: 'queued' as const }));

        await expect(
            runPostDownloadSync('a.jpg', {
                syncPixivImageToLocal: mock(async () => {
                    throw new Error('mongo down');
                }),
                syncMinioAndMaybeSubmitTagger: enqueue,
            }),
        ).rejects.toThrow('mongo down');

        expect(enqueue).not.toHaveBeenCalled();
    });

    it('turns a durable enqueue failure into a rejected post-download handoff', async () => {
        await expect(
            runPostDownloadSync('a.jpg', {
                syncPixivImageToLocal: mock(async () => ({ status: 'synced' as const, count: 1 })),
                syncMinioAndMaybeSubmitTagger: mock(async () => ({
                    status: 'enqueue_failed' as const,
                    error: 'result mongo down',
                })),
            }),
        ).rejects.toThrow('result mongo down');
    });

    it('allows explicitly disabled optional stages', async () => {
        const result = await runPostDownloadSync('a.jpg', {
            syncPixivImageToLocal: mock(async () => ({ status: 'disabled' as const })),
            syncMinioAndMaybeSubmitTagger: mock(async () => ({ status: 'tagger_disabled' as const })),
        });

        expect(result).toEqual({
            status: 'tagger_disabled',
            pixiv_image_mirror_status: 'disabled',
        });
    });
});
