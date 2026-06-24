import { describe, it, expect } from 'bun:test';
import { enqueueFilePipelinePosts } from './file-pipeline';

// enqueueFilePipelinePosts is the testable core: given file keys + a poster, it
// fires one POST per file key to the file-pipeline process endpoint and swallows
// per-post errors (best-effort, never throws into the inbound flow).

function makePoster() {
    const calls: Array<{ path: string; body: any; headers: any }> = [];
    const poster = async (path: string, body: any, opts: { headers: any }) => {
        calls.push({ path, body, headers: opts.headers });
    };
    return { poster, calls };
}

describe('enqueueFilePipelinePosts', () => {
    it('posts {message_id, file_key} per file key to the file-pipeline process endpoint', async () => {
        const { poster, calls } = makePoster();
        await enqueueFilePipelinePosts({
            messageId: 'om_1',
            fileKeys: ['file_a', 'file_b'],
            botName: 'chiwei',
            innerSecret: 'sek',
            lane: undefined,
            post: poster,
        });

        expect(calls.length).toBe(2);
        expect(calls[0].path).toBe('/api/file-pipeline/process');
        expect(calls[0].body).toEqual({ message_id: 'om_1', file_key: 'file_a' });
        expect(calls[1].body).toEqual({ message_id: 'om_1', file_key: 'file_b' });
        // auth + persona routing headers, mirroring the image pipeline
        expect(calls[0].headers.Authorization).toBe('Bearer sek');
        expect(calls[0].headers['X-App-Name']).toBe('chiwei');
    });

    it('injects x-ctx-lane from the pod lane so the fire-and-forget routes to the lane tool-service (not prod)', async () => {
        // Root cause of "file never cached on coe": the dev-bot webhook reaches the
        // coe channel-server without an x-ctx-lane header, so the request-scoped
        // context lane is empty and the laneRouter routes this background call to
        // prod tool-service (which lacks /api/file-pipeline/process -> 404). The
        // reliable lane is the pod's static LANE, threaded here explicitly.
        const { poster, calls } = makePoster();
        await enqueueFilePipelinePosts({
            messageId: 'om_1',
            fileKeys: ['file_a'],
            botName: 'chiwei',
            innerSecret: 'sek',
            lane: 'coe-world-life2',
            post: poster,
        });
        expect(calls[0].headers['x-ctx-lane']).toBe('coe-world-life2');
    });

    it('omits x-ctx-lane on prod (no lane) so the call stays laneless', async () => {
        const { poster, calls } = makePoster();
        await enqueueFilePipelinePosts({
            messageId: 'om_1',
            fileKeys: ['file_a'],
            botName: 'chiwei',
            innerSecret: 'sek',
            lane: undefined,
            post: poster,
        });
        expect(calls[0].headers['x-ctx-lane']).toBeUndefined();
    });

    it('does nothing when there are no file keys', async () => {
        const { poster, calls } = makePoster();
        await enqueueFilePipelinePosts({
            messageId: 'om_1',
            fileKeys: [],
            botName: 'chiwei',
            innerSecret: 'sek',
            lane: undefined,
            post: poster,
        });
        expect(calls.length).toBe(0);
    });

    it('swallows a failing post and still fires the rest (best-effort)', async () => {
        const calls: string[] = [];
        const poster = async (_path: string, body: any) => {
            calls.push(body.file_key);
            if (body.file_key === 'bad') throw new Error('tool-service 500');
        };
        // must not throw
        await enqueueFilePipelinePosts({
            messageId: 'om_1',
            fileKeys: ['bad', 'good'],
            botName: 'chiwei',
            innerSecret: 'sek',
            lane: undefined,
            post: poster,
        });
        expect(calls).toEqual(['bad', 'good']);
    });
});
