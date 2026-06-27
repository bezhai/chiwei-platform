import { describe, it, expect } from 'bun:test';
import { ed25519Sign, verifyWebhookSignature, signValidationResponse } from './webhook-verify';

const BOT_SECRET = 'abcd1234'; // short on purpose: must be padded/truncated to 32-byte seed

describe('webhook-verify: Ed25519 seed derivation', () => {
    it('produces a deterministic signature for the same secret + message (Ed25519 is deterministic)', () => {
        const msg = Buffer.from('hello-qq', 'utf-8');
        const a = ed25519Sign(BOT_SECRET, msg);
        const b = ed25519Sign(BOT_SECRET, msg);
        expect(a).toBe(b);
        // hex of a 64-byte Ed25519 signature => 128 hex chars
        expect(a).toMatch(/^[0-9a-f]{128}$/);
    });

    it('different secrets yield different signatures for the same message', () => {
        const msg = Buffer.from('hello-qq', 'utf-8');
        expect(ed25519Sign('secret-one', msg)).not.toBe(ed25519Sign('secret-two', msg));
    });
});

describe('webhook-verify: verifyWebhookSignature (op:0 event auth)', () => {
    const timestamp = '1700000000';
    const body = Buffer.from(JSON.stringify({ op: 0, t: 'C2C_MESSAGE_CREATE', d: { id: 'm1' } }), 'utf-8');
    // QQ signs `timestamp + body`
    const signature = ed25519Sign(BOT_SECRET, Buffer.concat([Buffer.from(timestamp, 'utf-8'), body]));

    it('accepts a valid signature', () => {
        expect(verifyWebhookSignature({ body, timestamp, signature, botSecret: BOT_SECRET })).toBe(true);
    });

    it('rejects a tampered body', () => {
        expect(
            verifyWebhookSignature({ body: Buffer.from('tampered', 'utf-8'), timestamp, signature, botSecret: BOT_SECRET }),
        ).toBe(false);
    });

    it('rejects a tampered timestamp', () => {
        expect(verifyWebhookSignature({ body, timestamp: '9999999999', signature, botSecret: BOT_SECRET })).toBe(false);
    });

    it('rejects a wrong bot secret', () => {
        expect(verifyWebhookSignature({ body, timestamp, signature, botSecret: 'totally-different' })).toBe(false);
    });

    it('returns false (does not throw) on malformed signature hex', () => {
        expect(verifyWebhookSignature({ body, timestamp, signature: 'not-hex-zzz', botSecret: BOT_SECRET })).toBe(false);
    });
});

describe('webhook-verify: signValidationResponse (op:13 handshake)', () => {
    const plainToken = 'plain-token-xyz';
    const eventTs = '1700000123';

    it('echoes plain_token and signs `event_ts + plain_token`, verifiable with the same public key', () => {
        const resp = signValidationResponse({ plainToken, eventTs, botSecret: BOT_SECRET });
        expect(resp.plain_token).toBe(plainToken);
        expect(resp.signature).toMatch(/^[0-9a-f]{128}$/);

        // The op:13 message is `event_ts + plain_token`. verifyWebhookSignature signs `timestamp + body`,
        // so feeding timestamp=event_ts and body=plain_token reconstructs the exact signed message.
        const verified = verifyWebhookSignature({
            body: Buffer.from(plainToken, 'utf-8'),
            timestamp: eventTs,
            signature: resp.signature,
            botSecret: BOT_SECRET,
        });
        expect(verified).toBe(true);
    });

    it('a different secret produces a signature that fails verification', () => {
        const resp = signValidationResponse({ plainToken, eventTs, botSecret: 'other-secret' });
        const verified = verifyWebhookSignature({
            body: Buffer.from(plainToken, 'utf-8'),
            timestamp: eventTs,
            signature: resp.signature,
            botSecret: BOT_SECRET,
        });
        expect(verified).toBe(false);
    });
});
