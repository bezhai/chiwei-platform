import { describe, it, expect } from 'bun:test';

import { handleChatResponse } from './chat-response-handler';
import type { ChatResponseHandlerDeps } from './chat-response-handler';
import { context } from '@middleware/context';
import type { OutboundCapabilities, MessageRef } from '@core/ports/channel-plugin';
import type { ConsumeMessage } from 'amqplib';

// chat-response-worker 的 lane 恢复口径。
//
// 背景：泳道队列（chat_response_{lane}）带 10s TTL + DLX，下游没部泳道时消息会
// 降级回 prod 队列，由 prod 的 chat-response-worker 接手。降级后这条消息**仍然
// 属于那个泳道**——出站要用泳道的下游，不能当成 prod 自己的活。AMQP header 是
// 唯一能穿过 TTL/DLX 降级还保持原样的载体（body 里的 lane 是 agent-service 写给
// 别的用途的字段，判 lane 不看它）。
//
// 口径（与 agent-service app/runtime/propagation.py 的 inject/extract 对齐）：
//   * header key 是 `lane`；
//   * 空串 / 非字符串 = 无 lane（等价 prod），因为 Python 侧 inject_context 对
//     「没有 lane」写的就是空串，两端必须同样归一；
//   * 不回落 body.lane：Python 的 _coerce 把「明确写空」和「压根没写」都归一成
//     None，消费侧无从区分，回落 body 会把上游已判定为 prod 的消息错误复活；
//   * 不回落 env LANE：prod worker 收到的很可能正是降级回来的泳道消息，env 兜底
//     会把它错判成 prod，恰好毁掉这里要修的能力。

function makeMsg(
    payload: Record<string, unknown>,
    headers?: Record<string, unknown>,
): ConsumeMessage {
    return {
        content: Buffer.from(JSON.stringify(payload)),
        fields: {} as ConsumeMessage['fields'],
        properties: { headers } as unknown as ConsumeMessage['properties'],
    } as ConsumeMessage;
}

// session_id=null 的主动发 payload：不碰 DB（handler 跳过 agent_response 查询），
// 用最小依赖把整条链跑到 context.run 内部。
function payloadWithBodyLane(bodyLane?: string) {
    return {
        channel: 'lark',
        is_proactive: true,
        message_id: 'proactive:550e8400-e29b-41d4-a716-446655440000',
        chat_id: '018f-conversation',
        bot_name: 'akao',
        session_id: null,
        root_id: null,
        is_p2p: true,
        content: '在吗',
        status: 'success',
        part_index: 0,
        is_last: true,
        ...(bodyLane === undefined ? {} : { lane: bodyLane }),
    };
}

// 在 context.run 内部观测 handler 实际取到的 lane。
// getCapabilities / 各 capability 方法都在 context.run 里被调用，随便挑一个都能
// 读到当时的 AsyncLocalStorage 内容，这里用 getCapabilities（最早的一个）。
function makeDeps(): { deps: ChatResponseHandlerDeps; observed: Array<string | undefined> } {
    const observed: Array<string | undefined> = [];
    const cap: OutboundCapabilities = {
        async resolveOutboundTarget() {
            throw new Error('must not resolve source message for proactive payload');
        },
        async resolveMessageRef() {
            throw new Error('must not resolve source message for proactive payload');
        },
        async resolveConversationRef(commonConversationId) {
            return { channelId: `oc_for_${commonConversationId}` };
        },
        async recordOutboundMessage() {
            return 'common_assistant_msg_id';
        },
        async sendText(): Promise<MessageRef> {
            return { channelId: 'om_new_msg' };
        },
        async reply(): Promise<MessageRef> {
            return { channelId: 'om_reply_msg' };
        },
    };
    const repo = {
        findOneBy: async () => null,
        update: async () => ({ affected: 0 }),
    } as unknown as ChatResponseHandlerDeps['repo'];

    return {
        observed,
        deps: {
            repo,
            getCapabilities: () => {
                observed.push(context.getAll().lane);
                return cap;
            },
            ack: () => {},
            nack: () => {},
            observeDuration: () => {},
            observeQueueDelay: () => {},
        },
    };
}

describe('handleChatResponse — lane 从 AMQP header 恢复', () => {
    it('header 带真实 lane：handler 在该 lane 的 context 下出站', async () => {
        const { deps, observed } = makeDeps();

        await handleChatResponse(deps, makeMsg(payloadWithBodyLane(), { lane: 'ppe-taskb' }));

        expect(observed).toEqual(['ppe-taskb']);
    });

    it('header lane 为空串：视为无 lane（prod），不回落 body.lane', async () => {
        const { deps, observed } = makeDeps();

        // agent-service inject_context 对「无 lane」写的就是空串，且 body 里可能
        // 仍带着 lane 字段——空串 header 必须压过 body。
        await handleChatResponse(deps, makeMsg(payloadWithBodyLane('ppe-taskb'), { lane: '' }));

        expect(observed).toEqual([undefined]);
    });

    it('完全没有 header：视为无 lane（prod），不回落 body.lane', async () => {
        const { deps, observed } = makeDeps();

        // 部署窗口内的在途旧消息（上游还没开始写 header）走这条路：按 lane 缺失
        // 处理、当 prod 走，这是已接受的降级，不是回落 body。
        await handleChatResponse(deps, makeMsg(payloadWithBodyLane('ppe-taskb'), undefined));

        expect(observed).toEqual([undefined]);
    });

    it('header 与 body 的 lane 不一致：header 是唯一权威', async () => {
        const { deps, observed } = makeDeps();

        await handleChatResponse(
            deps,
            makeMsg(payloadWithBodyLane('ppe-stale'), { lane: 'ppe-taskb' }),
        );

        expect(observed).toEqual(['ppe-taskb']);
    });

    it('header lane 不是字符串：视为无 lane（与 Python _coerce 对齐）', async () => {
        const { deps, observed } = makeDeps();

        await handleChatResponse(deps, makeMsg(payloadWithBodyLane('ppe-taskb'), { lane: 42 }));

        expect(observed).toEqual([undefined]);
    });
});
