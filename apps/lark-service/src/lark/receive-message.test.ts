// 一条飞书消息进来之后，本服务按什么顺序做事。
//
// 这里钉的就是「落账成功了才跑规则」这一条 —— 它是与拆分前的一处**有意**的差别，
// 见 receive-message.ts 顶部。

import { describe, expect, it } from 'bun:test';

import type { LarkEvent } from './ingress/lark-event';
import type { LarkBotLookup } from './message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from './message/read-message-event';
import type { LarkMessageEvent } from './message/wire';
import type { LarkInboundProjection } from './projection/inbound-projection';
import { receiveLarkMessage, type LarkReceiveDeps } from './receive-message';

const bots: LarkBotLookup = { byAppId: () => null, byUnionId: () => null };

function reading(): LarkMessageReading {
    const event: LarkMessageEvent = {
        app_id: 'cli_chiwei',
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: '{"text":"hi"}',
        },
    };
    return readLarkMessageEvent(event, bots)!;
}

const event: LarkEvent = {
    type: 'im.message.receive_v1',
    payload: {},
    botName: 'chiwei',
    traceId: 'trace-1',
};

const projection: LarkInboundProjection = {
    commonUserId: 'cu_sender',
    commonConversationId: 'cc_1',
    commonMessageId: 'cm_1',
    commonRootMessageId: 'cm_1',
    commonReplyMessageId: undefined,
    mentionedCommonUserIds: [],
};

function wire(overrides: Partial<LarkReceiveDeps> = {}) {
    const trace: string[] = [];
    const sawProjection: LarkInboundProjection[] = [];
    const deps: LarkReceiveDeps = {
        project: async () => {
            trace.push('project');
            return { kind: 'recorded', projection };
        },
        applyRules: async (_reading, seen) => {
            trace.push('rules');
            sawProjection.push(seen);
        },
        ...overrides,
    };
    return { deps, trace, sawProjection };
}

describe('receiveLarkMessage', () => {
    it('先落账，再跑规则', async () => {
        const wired = wire();

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.trace).toEqual(['project', 'rules']);
    });

    it('规则拿到的是投影产出的那组公共层 id', async () => {
        const wired = wire();

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.sawProjection).toEqual([projection]);
    });

    // 与拆分前的差别（有意）：拆分前规则跑在落库之前，落库失败时用户已经看到了指令
    // 的回复、库里却没有这条消息。这里落账失败则规则根本不跑，错误照常往上抛 ——
    // 泳道那条路据此重投，飞书那两个入口留下一条可查错误。
    it('落账失败时规则根本不跑，错误往上抛', async () => {
        const wired = wire({
            project: async () => {
                throw new Error('common_message is on fire');
            },
        });

        await expect(receiveLarkMessage(wired.deps, reading(), event)).rejects.toThrow(
            'common_message is on fire',
        );
        expect(wired.trace).toEqual([]);
    });

    // 交给别的泳道的消息本进程不再处理：规则、指令、chat.request 全归目标泳道。
    it('这条该走别的泳道时到此为止', async () => {
        const wired = wire({
            project: async () => {
                wired.trace.push('project');
                return { kind: 'handed-off', lane: 'ppe-x' };
            },
        });

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.trace).toEqual(['project']);
    });
});
