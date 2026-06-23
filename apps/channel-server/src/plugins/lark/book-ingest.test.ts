import { describe, it, expect } from 'bun:test';
import type { ContentItem } from '@core/channels/contracts';
import {
    isBookFilename,
    selectBookFile,
    forwardBookFile,
    type BookForwardDeps,
} from './book-ingest';

// ---------------------------------------------------------------------------
// isBookFilename — 纯判定：txt / epub 才当书（大小写不敏感）
// ---------------------------------------------------------------------------
describe('isBookFilename', () => {
    it('accepts .txt and .epub regardless of case', () => {
        expect(isBookFilename('斜阳.txt')).toBe(true);
        expect(isBookFilename('book.EPUB')).toBe(true);
        expect(isBookFilename('a.Txt')).toBe(true);
    });
    it('rejects non-book files', () => {
        expect(isBookFilename('doc.pdf')).toBe(false);
        expect(isBookFilename('v.mp4')).toBe(false);
        expect(isBookFilename('noext')).toBe(false);
        expect(isBookFilename(undefined)).toBe(false);
    });
});

// ---------------------------------------------------------------------------
// selectBookFile — 分流：只有 p2p 私聊里的 txt/epub 文件消息才走书接入路径
// ---------------------------------------------------------------------------
function fileItem(file_name?: string): ContentItem {
    return { kind: 'file', key: 'file_k', meta: { file_name, lark_type: 'file' } };
}

describe('selectBookFile', () => {
    it('selects a p2p txt/epub file message', () => {
        const sel = selectBookFile([fileItem('斜阳.txt')], 'direct');
        expect(sel).not.toBeNull();
        expect(sel!.fileKey).toBe('file_k');
        expect(sel!.fileName).toBe('斜阳.txt');
    });

    it('ignores file messages in group chats (book path is p2p-only)', () => {
        expect(selectBookFile([fileItem('斜阳.txt')], 'group')).toBeNull();
    });

    it('ignores non-book files (pdf/media) — they fall through to existing handling', () => {
        expect(selectBookFile([fileItem('doc.pdf')], 'direct')).toBeNull();
        expect(
            selectBookFile(
                [{ kind: 'file', key: 'm', meta: { file_name: 'v.mp4', lark_type: 'media' } }],
                'direct',
            ),
        ).toBeNull();
    });

    it('ignores text messages (no file)', () => {
        expect(selectBookFile([{ kind: 'text', text: 'hi' }], 'direct')).toBeNull();
    });
});

// ---------------------------------------------------------------------------
// forwardBookFile — 下载飞书文件 → base64 → POST agent-service；失败回真人
// ---------------------------------------------------------------------------
function makeDeps(overrides: Partial<BookForwardDeps> = {}): {
    deps: BookForwardDeps;
    calls: {
        download: Array<{ messageId: string; fileKey: string }>;
        fetch: Array<{ path: string; body: any }>;
        reply: string[];
    };
} {
    const calls = {
        download: [] as Array<{ messageId: string; fileKey: string }>,
        fetch: [] as Array<{ path: string; body: any }>,
        reply: [] as string[],
    };
    const deps: BookForwardDeps = {
        downloadFile: async (messageId, fileKey) => {
            calls.download.push({ messageId, fileKey });
            return Buffer.from('hello-book-bytes');
        },
        postIngest: async (path, body) => {
            calls.fetch.push({ path, body });
            return { ok: true, status: 200, json: { ok: true, book_id: 'b1', title: '斜阳' } };
        },
        replyToUser: async (msg) => {
            calls.reply.push(msg);
        },
        ...overrides,
    };
    return { deps, calls };
}

const baseInput = {
    lane: 'coe-t1',
    botName: 'chiwei',
    messageId: 'om_1',
    fileKey: 'file_k',
    fileName: '斜阳.txt',
};

describe('forwardBookFile', () => {
    it('downloads the file and posts base64 + metadata to agent-service', async () => {
        const { deps, calls } = makeDeps();
        await forwardBookFile(baseInput, deps);

        expect(calls.download).toEqual([{ messageId: 'om_1', fileKey: 'file_k' }]);
        expect(calls.fetch.length).toBe(1);
        expect(calls.fetch[0].path).toBe('/api/internal/book/ingest');
        const body = calls.fetch[0].body;
        expect(body.lane).toBe('coe-t1');
        expect(body.bot_name).toBe('chiwei');
        expect(body.filename).toBe('斜阳.txt');
        // file bytes are base64 of the downloaded buffer
        expect(body.file_b64).toBe(Buffer.from('hello-book-bytes').toString('base64'));
        // success → no error reply to user
        expect(calls.reply).toEqual([]);
    });

    it('replies a failure hint to the user when agent-service reports parse failure', async () => {
        const { deps, calls } = makeDeps({
            postIngest: async () => ({
                ok: true,
                status: 200,
                json: { ok: false, reason: '这个文件没能解析成一本书' },
            }),
        });
        await forwardBookFile({ ...baseInput, fileName: 'broken.epub' }, deps);

        expect(calls.reply.length).toBe(1);
        expect(calls.reply[0]).toContain('没能解析');
    });

    it('replies a failure hint when the HTTP call itself fails (non-2xx)', async () => {
        const { deps, calls } = makeDeps({
            postIngest: async () => ({ ok: false, status: 500, json: null }),
        });
        await forwardBookFile(baseInput, deps);
        expect(calls.reply.length).toBe(1);
    });

    it('replies a failure hint when download throws (does not crash the inbound flow)', async () => {
        const { deps, calls } = makeDeps({
            downloadFile: async () => {
                throw new Error('feishu download 403');
            },
        });
        await forwardBookFile(baseInput, deps);
        expect(calls.fetch).toEqual([]); // never posted
        expect(calls.reply.length).toBe(1);
    });
});
