import { describe, expect, it } from 'bun:test';
import { buildImageByPixivAddrFilter } from './service';

// buildImageByPixivAddrFilter is a pure query builder so it can be unit-tested
// without importing ./client (which would trigger a real Mongo connect) and
// without mock.module (which pollutes sibling tests in the same bun run).
describe('buildImageByPixivAddrFilter', () => {
    it('empty pixivAddr -> null (caller short-circuits to null doc)', () => {
        expect(buildImageByPixivAddrFilter('')).toBeNull();
    });

    it('non-empty pixivAddr -> matches pixiv_addr AND requires a non-empty tos_file_name', () => {
        const filter = buildImageByPixivAddrFilter('123_p0.png');

        expect(filter).not.toBeNull();
        expect(filter!.pixiv_addr).toBe('123_p0.png');

        // a doc whose tos_file_name is missing or empty must NOT match this filter,
        // so the $nin guard against null/'' must be present
        expect(filter!.tos_file_name).toEqual({ $nin: [null, ''] } as any);
    });
});
