import { describe, it, expect } from 'bun:test';
import type { InboundMessage } from '@core/channels/contracts';
import { qqAddressing } from './addressing';
import { QQ_SELF_MENTION_TARGET } from './inbound';

function inbound(scope: string, hints: string[]): InboundMessage {
    return {
        channel: 'qq',
        bot_name: 'chiwei-qq',
        channel_message_id: 'm',
        channel_chat_id: 'c',
        channel_user_id: 'u',
        conversation_scope: scope,
        thread_ref: null,
        addressing_hints: hints.map((targetId) => ({ targetId })),
        content: [{ kind: 'text', text: 'x' }],
        received_at: 0,
    };
}

describe('qqAddressing.decide', () => {
    it('direct always responds, with non-empty reason', () => {
        const d = qqAddressing.decide(inbound('direct', []), QQ_SELF_MENTION_TARGET);
        expect(d.respond).toBe(true);
        expect(d.reason.length).toBeGreaterThan(0);
    });

    it('group with @bot (self mention) responds', () => {
        const d = qqAddressing.decide(
            inbound('group', ['member_x', QQ_SELF_MENTION_TARGET]),
            QQ_SELF_MENTION_TARGET,
        );
        expect(d.respond).toBe(true);
        expect(d.reason.length).toBeGreaterThan(0);
    });

    it('group without @bot does not respond, with non-empty reason', () => {
        const d = qqAddressing.decide(inbound('group', ['member_x']), QQ_SELF_MENTION_TARGET);
        expect(d.respond).toBe(false);
        expect(d.reason.length).toBeGreaterThan(0);
    });
});
