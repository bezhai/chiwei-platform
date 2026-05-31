import { describe, it, expect } from 'bun:test';
import type { InboundMessage } from '@core/channels/contracts';
import { larkAddressing } from './addressing';

// NeedRobotMention equivalence: respond iff p2p (direct) OR bot mentioned in group.
describe('larkAddressing.decide (NeedRobotMention equivalence)', () => {
    function inbound(scope: string, hints: string[]): InboundMessage {
        return {
            channel: 'lark',
            bot_name: 'b',
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

    it('p2p (direct) always responds', () => {
        const d = larkAddressing.decide(inbound('direct', []), 'bot_union');
        expect(d.respond).toBe(true);
        expect(d.reason.length).toBeGreaterThan(0);
    });

    it('group with bot mention responds', () => {
        const d = larkAddressing.decide(inbound('group', ['bot_union']), 'bot_union');
        expect(d.respond).toBe(true);
        expect(d.reason.length).toBeGreaterThan(0);
    });

    it('group without bot mention does not respond, with non-empty reason', () => {
        const d = larkAddressing.decide(inbound('group', ['someone_else']), 'bot_union');
        expect(d.respond).toBe(false);
        expect(d.reason.length).toBeGreaterThan(0);
    });
});
