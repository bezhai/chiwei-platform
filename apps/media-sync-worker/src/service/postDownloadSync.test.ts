import { describe, expect, it, mock } from 'bun:test';
import { runPostDownloadSync } from './postDownloadSync';

describe('runPostDownloadSync', () => {
    it('hands the page to the durable tagger outbox', async () => {
        const handoff = mock(async () => ({ status: 'queued' as const }));

        const result = await runPostDownloadSync('a.jpg', { syncMinioAndMaybeSubmitTagger: handoff });

        expect(handoff).toHaveBeenCalledWith('a.jpg');
        expect(result).toEqual({ status: 'queued' });
    });

    it('turns a durable enqueue failure into a rejected post-download handoff', async () => {
        await expect(
            runPostDownloadSync('a.jpg', {
                syncMinioAndMaybeSubmitTagger: mock(async () => ({
                    status: 'enqueue_failed' as const,
                    error: 'result mongo down',
                })),
            }),
        ).rejects.toThrow('result mongo down');
    });

    it('allows an explicitly disabled tagger', async () => {
        const result = await runPostDownloadSync('a.jpg', {
            syncMinioAndMaybeSubmitTagger: mock(async () => ({ status: 'tagger_disabled' as const })),
        });

        expect(result).toEqual({ status: 'tagger_disabled' });
    });
});
