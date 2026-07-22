import { describe, expect, it } from 'bun:test';
import { assertPageDownloadsSucceeded, type PageDownloadOutcome } from './pageDownloadResult';

function outcome(status: PageDownloadOutcome['status'], error?: string): PageDownloadOutcome {
    return { status, error };
}

describe('assertPageDownloadsSucceeded', () => {
    it('accepts downloaded pages and existing pages whose post-sync replay completed', () => {
        expect(() =>
            assertPageDownloadsSucceeded('100', [
                outcome('downloaded'),
                outcome('exists'),
            ]),
        ).not.toThrow();
    });

    it.each([
        ['missing_url', undefined],
        ['download_failed', 'proxy timeout'],
        ['add_image_failed', 'mongo unavailable'],
        ['post_sync_failed', 'mirror unavailable'],
    ] as const)('rejects a page with status %s', (status, error) => {
        expect(() =>
            assertPageDownloadsSucceeded('100', [
                outcome('downloaded'),
                outcome(status, error),
            ]),
        ).toThrow(error ?? status);
    });

    it('summarizes every failed page after concurrent work settles', () => {
        expect(() =>
            assertPageDownloadsSucceeded('100', [
                outcome('download_failed', 'first'),
                outcome('exists'),
                outcome('post_sync_failed', 'third'),
            ]),
        ).toThrow('page 1 download_failed: first; page 3 post_sync_failed: third');
    });
});
