import { describe, it, expect } from 'bun:test';
import { PassiveWindowManager, InMemoryPassiveWindowStore } from './manager';

const WINDOW_MS = 60 * 60 * 1000;

function mk(nowRef: { t: number }) {
    const store = new InMemoryPassiveWindowStore();
    const mgr = new PassiveWindowManager(store, {
        windowMs: WINDOW_MS,
        maxReplies: 4,
        now: () => nowRef.t,
    });
    return { store, mgr };
}

describe('PassiveWindowManager: active send', () => {
    it('drops messages without replyToMessageId and never consumes idempotency', async () => {
        const nowRef = { t: 1_700_000_000_000 };
        const { mgr } = mk(nowRef);
        const r = await mgr.reserve({ botName: 'chiwei', idempotencyKey: 'k1' });
        expect(r).toEqual({ action: 'drop', reason: 'active_send' });
    });
});

describe('PassiveWindowManager: passive reply seq + multi-part', () => {
    it('assigns incrementing msgSeq for the same msgId across distinct idempotency keys', async () => {
        const nowRef = { t: 1_700_000_000_000 };
        const { mgr } = mk(nowRef);
        const seqs: number[] = [];
        for (let i = 0; i < 4; i++) {
            const r = await mgr.reserve({
                botName: 'chiwei',
                replyToMessageId: 'MSG_A',
                idempotencyKey: `MSG_A:${i}`,
            });
            expect(r.action).toBe('send');
            if (r.action === 'send') seqs.push(r.msgSeq);
        }
        expect(seqs).toEqual([1, 2, 3, 4]);
    });

    it('drops the 5th reply to the same msgId as limit_exceeded', async () => {
        const nowRef = { t: 1_700_000_000_000 };
        const { mgr } = mk(nowRef);
        for (let i = 0; i < 4; i++) {
            await mgr.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_B', idempotencyKey: `MSG_B:${i}` });
        }
        const fifth = await mgr.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_B', idempotencyKey: 'MSG_B:4' });
        expect(fifth).toEqual({ action: 'drop', reason: 'limit_exceeded' });
    });

    it('isolates seq counters per (botName, msgId)', async () => {
        const nowRef = { t: 1_700_000_000_000 };
        const { mgr } = mk(nowRef);
        const a = await mgr.reserve({ botName: 'chiwei', replyToMessageId: 'X', idempotencyKey: 'a' });
        const b = await mgr.reserve({ botName: 'meimei', replyToMessageId: 'X', idempotencyKey: 'b' });
        expect(a).toEqual({ action: 'send', msgSeq: 1 });
        expect(b).toEqual({ action: 'send', msgSeq: 1 });
    });
});

describe('PassiveWindowManager: idempotency', () => {
    it('drops a redelivered idempotency key without consuming a seq', async () => {
        const nowRef = { t: 1_700_000_000_000 };
        const { mgr } = mk(nowRef);
        const first = await mgr.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_C', idempotencyKey: 'dup' });
        expect(first).toEqual({ action: 'send', msgSeq: 1 });

        const dup = await mgr.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_C', idempotencyKey: 'dup' });
        expect(dup).toEqual({ action: 'drop', reason: 'duplicate' });

        // a fresh key must get seq 2 (proving the duplicate did NOT consume seq 2)
        const next = await mgr.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_C', idempotencyKey: 'fresh' });
        expect(next).toEqual({ action: 'send', msgSeq: 2 });
    });
});

describe('PassiveWindowManager: 60min window', () => {
    it('drops replies after the window elapses', async () => {
        const nowRef = { t: 1_700_000_000_000 };
        const { mgr } = mk(nowRef);
        const first = await mgr.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_D', idempotencyKey: 'd0' });
        expect(first.action).toBe('send');

        nowRef.t += WINDOW_MS + 1; // one ms past the window
        const late = await mgr.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_D', idempotencyKey: 'd1' });
        expect(late).toEqual({ action: 'drop', reason: 'window_expired' });
    });
});

describe('PassiveWindowManager: persistence across restart', () => {
    it('a new manager sharing the store continues the same window state', async () => {
        const nowRef = { t: 1_700_000_000_000 };
        const store = new InMemoryPassiveWindowStore();
        const mgr1 = new PassiveWindowManager(store, { windowMs: WINDOW_MS, maxReplies: 4, now: () => nowRef.t });
        await mgr1.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_E', idempotencyKey: 'e0' });
        await mgr1.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_E', idempotencyKey: 'e1' });

        // simulate restart: brand new manager, same backing store
        const mgr2 = new PassiveWindowManager(store, { windowMs: WINDOW_MS, maxReplies: 4, now: () => nowRef.t });
        const r = await mgr2.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_E', idempotencyKey: 'e2' });
        expect(r).toEqual({ action: 'send', msgSeq: 3 });

        // and idempotency survives restart too
        const dup = await mgr2.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_E', idempotencyKey: 'e0' });
        expect(dup).toEqual({ action: 'drop', reason: 'duplicate' });
    });
});

describe('PassiveWindowManager: concurrency', () => {
    it('serializes concurrent reserves for the same msgId (no duplicate seq)', async () => {
        const nowRef = { t: 1_700_000_000_000 };
        const { mgr } = mk(nowRef);
        const results = await Promise.all(
            Array.from({ length: 4 }, (_, i) =>
                mgr.reserve({ botName: 'chiwei', replyToMessageId: 'MSG_F', idempotencyKey: `f${i}` }),
            ),
        );
        const seqs = results
            .filter((r): r is { action: 'send'; msgSeq: number } => r.action === 'send')
            .map((r) => r.msgSeq)
            .sort((a, b) => a - b);
        expect(seqs).toEqual([1, 2, 3, 4]);
    });
});
