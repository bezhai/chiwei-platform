import { describe, it, expect } from 'bun:test';
import { decidePassiveReply, type WindowRecord } from './decision';

const WINDOW_MS = 60 * 60 * 1000; // 60min
const MAX = 4;
const NOW = 1_700_000_000_000;

function base(overrides: Partial<Parameters<typeof decidePassiveReply>[0]> = {}) {
    return {
        hasReplyTo: true,
        idempotencyAlreadySeen: false,
        record: null as WindowRecord | null,
        now: NOW,
        windowMs: WINDOW_MS,
        maxReplies: MAX,
        ...overrides,
    };
}

describe('decidePassiveReply: active-send guard', () => {
    it('drops when there is no replyToMessageId (active send)', () => {
        const d = decidePassiveReply(base({ hasReplyTo: false }));
        expect(d).toEqual({ action: 'drop', reason: 'active_send' });
    });

    it('active-send guard wins even if idempotency already seen', () => {
        const d = decidePassiveReply(base({ hasReplyTo: false, idempotencyAlreadySeen: true }));
        expect(d.action).toBe('drop');
        expect(d).toMatchObject({ reason: 'active_send' });
    });
});

describe('decidePassiveReply: idempotency', () => {
    it('drops duplicates', () => {
        const d = decidePassiveReply(base({ idempotencyAlreadySeen: true }));
        expect(d).toEqual({ action: 'drop', reason: 'duplicate' });
    });
});

describe('decidePassiveReply: seq increment within window', () => {
    it('first reply gets msgSeq=1 and starts the window at now', () => {
        const d = decidePassiveReply(base({ record: null }));
        expect(d).toEqual({
            action: 'send',
            msgSeq: 1,
            nextRecord: { windowStart: NOW, replies: 1 },
        });
    });

    it('subsequent replies increment msgSeq and preserve windowStart', () => {
        const rec: WindowRecord = { windowStart: NOW - 1000, replies: 1 };
        const d = decidePassiveReply(base({ record: rec }));
        expect(d).toEqual({
            action: 'send',
            msgSeq: 2,
            nextRecord: { windowStart: NOW - 1000, replies: 2 },
        });
    });

    it('multi-part replies walk 1,2,3,4 then the 5th is dropped', () => {
        let rec: WindowRecord | null = null;
        const seqs: number[] = [];
        for (let i = 0; i < 4; i++) {
            const d = decidePassiveReply(base({ record: rec }));
            expect(d.action).toBe('send');
            if (d.action === 'send') {
                seqs.push(d.msgSeq);
                rec = d.nextRecord;
            }
        }
        expect(seqs).toEqual([1, 2, 3, 4]);
        const fifth = decidePassiveReply(base({ record: rec }));
        expect(fifth).toEqual({ action: 'drop', reason: 'limit_exceeded' });
    });
});

describe('decidePassiveReply: 60min window expiry', () => {
    it('still allows a reply exactly at the window edge (now - start == windowMs)', () => {
        const rec: WindowRecord = { windowStart: NOW - WINDOW_MS, replies: 1 };
        const d = decidePassiveReply(base({ record: rec }));
        expect(d.action).toBe('send');
    });

    it('drops a reply one ms past the window', () => {
        const rec: WindowRecord = { windowStart: NOW - WINDOW_MS - 1, replies: 1 };
        const d = decidePassiveReply(base({ record: rec }));
        expect(d).toEqual({ action: 'drop', reason: 'window_expired' });
    });

    it('window expiry takes priority over the count limit', () => {
        const rec: WindowRecord = { windowStart: NOW - WINDOW_MS - 1, replies: MAX };
        const d = decidePassiveReply(base({ record: rec }));
        expect(d).toEqual({ action: 'drop', reason: 'window_expired' });
    });
});
