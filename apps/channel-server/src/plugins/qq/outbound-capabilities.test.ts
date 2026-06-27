import { describe, it, expect, mock, beforeEach } from 'bun:test';
import type { CustomOutboundMessage } from '@inner/shared/protocols';
import type { ContentItem, ThreadRef } from '@core/channels/contracts';

// reverse-resolve is the plugin's private DB layer; mock it.
const reverse = {
    resolveQqMessageRef: mock(async (_id: string) => 'src_qq_msg'),
    resolveQqConversationRef: mock(async (_id: string) => ({ channelId: 'qq_conv' })),
    reverseResolveOutbound: mock(async () => ({
        channelMessageId: 'src_qq_msg',
        channelChatId: 'qq_conv',
        channelRootId: undefined,
    })),
};
mock.module('./outbound-reverse-resolve', () => reverse);

mock.module('@middleware/context', () => ({
    context: { getBotName: () => 'chiwei-qq' },
}));

const { createQqOutboundCapabilities } = await import('./outbound-capabilities');
import type { QqOutboundDeps } from './outbound-capabilities';

function makeDeps(
    result: Awaited<ReturnType<QqOutboundDeps['postOutbound']>> = {
        sent: true,
        messageId: 'gateway_returned_id',
    },
): { deps: QqOutboundDeps; sent: CustomOutboundMessage[] } {
    const sent: CustomOutboundMessage[] = [];
    const deps: QqOutboundDeps = {
        async postOutbound(msg) {
            sent.push(msg);
            return result;
        },
    };
    return { deps, sent };
}

const text = (s: string): ContentItem[] => [{ kind: 'text', text: s }];

beforeEach(() => {
    reverse.resolveQqMessageRef.mockClear();
    reverse.resolveQqMessageRef.mockImplementation(async () => 'src_qq_msg');
});

describe('qq OutboundCapabilities.reply', () => {
    it('produces a passive CustomOutboundMessage: replyToMessageId from thread anchor, conv/text/chatType from ctx/content', async () => {
        const { deps, sent } = makeDeps();
        const cap = createQqOutboundCapabilities(deps);
        const thread: ThreadRef = { selfChannelMessageId: 'src_qq_msg' };

        const ref = await cap.reply(thread, text('你好'), {
            imageRegistryId: 'common_src_1',
            groupConversationId: 'qq_conv',
            resolveMentions: true,
        });

        expect(sent.length).toBe(1);
        expect(sent[0].botName).toBe('chiwei-qq');
        expect(sent[0].replyToMessageId).toBe('src_qq_msg');
        expect(sent[0].conversationId).toBe('qq_conv');
        expect(sent[0].chatType).toBe('group');
        expect(sent[0].text).toBe('你好');
        expect(sent[0].idempotencyKey.length).toBeGreaterThan(0);
        expect(ref.channelId).toBe('gateway_returned_id');
    });

    it('direct chat (resolveMentions=false) sets chatType=direct', async () => {
        const { deps, sent } = makeDeps();
        const cap = createQqOutboundCapabilities(deps);
        await cap.reply({ selfChannelMessageId: 'src_qq_msg' }, text('hi'), {
            imageRegistryId: 'c1',
            groupConversationId: 'qq_conv',
            resolveMentions: false,
        });
        expect(sent[0].chatType).toBe('direct');
    });

    it('fail-loud when thread has no usable anchor (no replyToMessageId)', async () => {
        const { deps } = makeDeps();
        const cap = createQqOutboundCapabilities(deps);
        await expect(
            cap.reply({ inThread: true } as ThreadRef, text('x'), {
                groupConversationId: 'qq_conv',
            }),
        ).rejects.toThrow();
    });
});

describe('qq OutboundCapabilities.sendText (continuation / proactive)', () => {
    it('continuation: reverse-resolves the source common id (ctx.imageRegistryId) to the original qq msg id', async () => {
        const { deps, sent } = makeDeps();
        const cap = createQqOutboundCapabilities(deps);

        const ref = await cap.sendText({ channelId: 'qq_conv' }, text('续段'), {
            imageRegistryId: 'common_src_1',
            groupConversationId: 'qq_conv',
            resolveMentions: true,
        });

        expect(reverse.resolveQqMessageRef).toHaveBeenCalledWith('common_src_1');
        expect(sent.length).toBe(1);
        expect(sent[0].replyToMessageId).toBe('src_qq_msg');
        expect(ref.channelId).toBe('gateway_returned_id');
    });

    it('two parts with identical text but different partIndex get distinct idempotencyKeys', async () => {
        const { deps, sent } = makeDeps();
        const cap = createQqOutboundCapabilities(deps);
        const ctxBase = {
            imageRegistryId: 'common_src_1',
            groupConversationId: 'qq_conv',
            resolveMentions: true,
        };

        await cap.sendText({ channelId: 'qq_conv' }, text('同样的话'), { ...ctxBase, partIndex: 1 });
        await cap.sendText({ channelId: 'qq_conv' }, text('同样的话'), { ...ctxBase, partIndex: 2 });

        expect(sent[0].idempotencyKey).toBe('qq:common_src_1:1');
        expect(sent[1].idempotencyKey).toBe('qq:common_src_1:2');
        expect(sent[0].idempotencyKey).not.toBe(sent[1].idempotencyKey);
    });

    it('proactive (no resolvable source msg id): THROWS, does not post to the gateway', async () => {
        const { deps, sent } = makeDeps();
        reverse.resolveQqMessageRef.mockImplementation(async () => {
            throw new Error('cannot resolve proactive:xxx');
        });
        const cap = createQqOutboundCapabilities(deps);

        await expect(
            cap.sendText({ channelId: 'qq_conv' }, text('主动发'), {
                imageRegistryId: 'proactive:abc',
                groupConversationId: 'qq_conv',
                resolveMentions: false,
            }),
        ).rejects.toThrow();

        // 反查不到 = 不发网关、抛错（handler catch 兜住、不 record），不污染 qq_message。
        expect(sent.length).toBe(0);
    });

    it('gateway returns sent:false → THROWS, does not return an empty channelId', async () => {
        const { deps, sent } = makeDeps({ sent: false, reason: 'over_window' });
        const cap = createQqOutboundCapabilities(deps);

        await expect(
            cap.sendText({ channelId: 'qq_conv' }, text('续段'), {
                imageRegistryId: 'common_src_1',
                groupConversationId: 'qq_conv',
                resolveMentions: true,
            }),
        ).rejects.toThrow(/over_window/);

        // 仍然把消息投给了网关（reserve 才知道超窗），但回执 sent:false 必须抛错。
        expect(sent.length).toBe(1);
    });
});

describe('qq OutboundCapabilities.reply: gateway drop is fail-loud', () => {
    it('gateway returns sent:false → THROWS, does not return an empty channelId', async () => {
        const { deps } = makeDeps({ sent: false, reason: 'max_replies_exceeded' });
        const cap = createQqOutboundCapabilities(deps);

        await expect(
            cap.reply({ selfChannelMessageId: 'src_qq_msg' }, text('回复'), {
                imageRegistryId: 'common_src_1',
                groupConversationId: 'qq_conv',
                resolveMentions: true,
            }),
        ).rejects.toThrow(/max_replies_exceeded/);
    });
});

describe('qq OutboundCapabilities resolve + record delegation', () => {
    it('resolveOutboundTarget maps reverse-resolved refs into ports shape', async () => {
        const { deps } = makeDeps();
        const cap = createQqOutboundCapabilities(deps);
        const out = await cap.resolveOutboundTarget({
            commonMessageId: 'cm',
            commonConversationId: 'cc',
        });
        expect(out.message.channelId).toBe('src_qq_msg');
        expect(out.conversation.channelId).toBe('qq_conv');
    });

    it('resolveConversationRef delegates to the qq reverse resolver', async () => {
        const { deps } = makeDeps();
        const cap = createQqOutboundCapabilities(deps);
        const conv = await cap.resolveConversationRef('cc');
        expect(conv.channelId).toBe('qq_conv');
    });
});
