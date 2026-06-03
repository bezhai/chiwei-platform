import { context } from '@middleware/context';
import { v4 as uuidv4 } from 'uuid';
import { v7 as uuidv7 } from 'uuid';
import { CommonAgentResponseRepository } from '@repositories/repositories';
import { CommonAgentResponse } from '@entities/common-agent-response';
import { getLane } from '@integrations/rabbitmq';
import type { RuleMessage } from 'core/rules/rule-message';
import type { RuleHandlerContext } from 'core/rules/engine';

// chat.request 载荷。message_id/chat_id/root_id/user_id 一律是 common_* id。
// agent-service 对 channel 无感知，只消费 common 口径。
export interface ChatRequestPayload {
    session_id: string;
    channel: string;
    message_id: string;
    chat_id: string;
    is_p2p: boolean;
    root_id: string;
    user_id: string;
    bot_name: string | undefined;
    is_canary: boolean;
    lane: string | undefined;
    enqueued_at: number;
    persona_ids: string[];
}

// chat.request 的 channel 专属富化字段（is_canary / persona_ids）。channel
// 插件负责把平台私有寻址信息收敛成 persona_id，core 的 reply.ts 不认识飞书对象。
// 未注入时取中性默认（is_canary=false / persona_ids=[]），绝不把平台绑定泄漏
// 到 agent-service。
export interface ChatRequestEnrichment {
    isCanary: boolean;
    personaIds: string[];
}

export type ChatRequestEnricher = (message: RuleMessage) => ChatRequestEnrichment;

const neutralEnricher: ChatRequestEnricher = () => ({
    isCanary: false,
    personaIds: [],
});

const chatRequestEnrichers = new Map<string, ChatRequestEnricher>();

// channel 插件 import 期注册"按本平台富化 chat.request"的实现。core 只按
// message.channel 找对应 enricher，不碰平台 SDK。
export function registerChatRequestEnricher(channel: string, fn: ChatRequestEnricher): void {
    chatRequestEnrichers.set(channel, fn);
}

// 测试钩子：清空注册表，避免跨用例污染。
export function resetChatRequestEnrichers(): void {
    chatRequestEnrichers.clear();
}

function chatRequestEnricherFor(channel: string): ChatRequestEnricher {
    return chatRequestEnrichers.get(channel) ?? neutralEnricher;
}

// 纯函数：从平台无关 RuleMessage 构造 chat.request 载荷。平台专属的寻址结果
// 必须在插件层收敛成 persona_ids，agent-service 不再解析 app_id/open_id。
export function buildChatRequestPayload(
    message: RuleMessage,
    sessionId: string,
    botName: string | undefined,
    lane: string | undefined,
): ChatRequestPayload {
    const { isCanary, personaIds } = chatRequestEnricherFor(message.channel)(message);
    return {
        session_id: sessionId,
        channel: message.channel,
        message_id: message.commonMessageId,
        chat_id: message.commonConversationId,
        is_p2p: message.isDirect,
        root_id: message.commonRootMessageId ?? message.commonMessageId,
        user_id: message.commonUserId,
        bot_name: botName,
        is_canary: isCanary,
        lane,
        enqueued_at: Date.now(),
        persona_ids: personaIds,
    };
}

/**
 * persona 文本主链路 —— 决策五里唯一真正平台无关的规则，消费 RuleMessage。
 *
 * 5b 入站重排（决策一/二）：本 handler 在 runRules 阶段**不实际 publish**、
 * **不取去重锁**、**不落 common_agent_response pending 行**。它只做平台无关的纯
 * 预备工作（生成 session_id、构造 chat.request 载荷、构造 pending 行落库
 * 闭包），把"待发 ChatTrigger 意图"经 ctx.registerPendingChatTrigger 登记
 * 给引擎，由接线点 handlers.ts 在 common/lark 入站消息写入成功之后取锁、抢到锁才
 * 落 pending 行并发 MQ —— 保证下游 agent-service find_message_content
 * (message_id) 先存后查、不读空走"未找到消息记录"短路；去重锁、pending
 * 行落库、publish 三者紧邻（避免拿锁后消息写入失败导致锁空占 60s）。
 *
 * 必改2：common_agent_response pending 行 save 后移到抢锁之后。重排前 setNx 在
 * pending save 之前、未抢锁的 bot 直接 return 不 save；若 pending save 仍
 * 留在 runRules 阶段（早于 handlers 后移的 setNx），多 bot 同群处理同一
 * 全局 message_id 时每个 bot 都写一条 pending 行、但只有抢锁的才 publish
 * → 未抢锁 bot 留下永不完成的孤儿 pending 行。故 save 闭包化、由 handlers
 * 抢锁后调用。common_agent_response 仓储逻辑仍只在本文件一处（闭包内），不泄漏到
 * handlers。pending 行 ⇎ 已发 MQ 不是系统不变量（消费方按 session_id /
 * trigger_message_id 查，只有 chat.response 真回来才命中，而那必在
 * publish 之后），故 save 时序后移安全（见回报 grep 求证）。
 */
export async function makeTextReply(message: RuleMessage, ctx?: RuleHandlerContext): Promise<void> {
    const sessionId = uuidv4();
    const botName = context.getBotName() || undefined;

    const lane = context.getLane() || getLane() || undefined;
    const payload = buildChatRequestPayload(message, sessionId, botName, lane);

    // common_agent_response pending 行落库闭包（必改2）：仓储逻辑只在此一处，
    // 但**不在此执行**。由 handlers.ts 抢到去重锁后调用，与 publish 原子
    // 相邻。落库失败仍记可查日志、不抛（与重排前同语义：pending 行只是
    // 观测便利，不是发 MQ 的前置不变量）。
    const savePending = async (): Promise<void> => {
        try {
            const agentResponse = CommonAgentResponseRepository.create({
                response_id: uuidv7(),
                session_id: sessionId,
                trigger_common_message_id: message.commonMessageId,
                common_conversation_id: message.commonConversationId,
                bot_name: botName,
                status: 'pending',
            } as Partial<CommonAgentResponse>);
            await CommonAgentResponseRepository.save(agentResponse);
        } catch (e) {
            console.error('Failed to create common_agent_response:', e);
        }
    };

    // 登记待发意图（决策一）。dedupeKey 用全局 common_message_id
    // （跨 channel 唯一），取锁、pending 落库、publish 由 handlers.ts 在
    // common/lark 入站消息写入成功后紧邻执行。ctx 缺失（理论上不会，handlers 必传）
    // 则不登记 —— 防御性健壮，绝不退回旧的"handler 内直接 publish"。
    ctx?.registerPendingChatTrigger({
        payload,
        lane,
        dedupeKey: `make_reply:${message.commonMessageId}`,
        savePending,
    });

    console.info(
        `[makeTextReply] Registered pending chat.request: session_id=${sessionId}, ` +
            `message_id=${message.commonMessageId}, channel=${message.channel}, ` +
            `lane=${lane || 'prod'}`,
    );
}
