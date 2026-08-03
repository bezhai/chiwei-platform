import { context } from '../middleware/context';
import { v4 as uuidv4 } from 'uuid';
import { v7 as uuidv7 } from 'uuid';
import { commonAgentResponseRepo } from '../persistence/repositories';
import { CommonAgentResponse } from '../entities/common-agent-response';
import { getLane } from '../mq/client';
import type { RuleMessage } from './rule-message';
import type { RuleHandlerContext } from './engine';

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

// chat.request 的渠道专属富化字段（is_canary / persona_ids）。渠道插件负责把
// 自己那套私有寻址信息收敛成 persona_id —— 本模块不认识任何渠道对象。未注入时
// 取中性默认（is_canary=false / persona_ids=[]），绝不把渠道绑定泄漏到
// agent-service。
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

// 渠道插件在 import 期注册"按本渠道富化 chat.request"的实现。本模块只按
// message.channel 找对应 enricher，不碰任何渠道 SDK。
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

// 纯函数：从渠道无关 RuleMessage 构造 chat.request 载荷。渠道专属的寻址结果
// 必须在插件层收敛成 persona_ids，agent-service 只消费 common 口径、不解析任何
// 渠道裸 id。
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
 * 聊天主链路 —— 规则集里唯一真正渠道无关、默认对所有渠道生效的 handler。
 *
 * 入站重排：本 handler 在 runRules 阶段**不实际 publish**、**不取去重锁**、
 * **不落 common_agent_response pending 行**。它只做渠道无关的纯预备工作（生成
 * session_id、构造 chat.request 载荷、构造 pending 行落库闭包），把"待发
 * ChatTrigger 意图"经 ctx.registerPendingChatTrigger 登记给引擎；由各渠道自己的
 * 入站接线点在该渠道的入站消息写入成功之后取锁、抢到锁才落 pending 行并发 MQ ——
 * 保证下游 agent-service 按 message_id 查消息内容时先存后查、不读空走"未找到
 * 消息记录"短路；去重锁、pending 行落库、publish 三者紧邻（避免拿锁后消息写入
 * 失败导致锁空占 60s）。
 *
 * pending 行为什么必须等到抢锁之后才 save：多个 bot 处理同一个群里的同一条消息
 * 时，只有抢到去重锁的那个才 publish。若 pending 行还留在 runRules 阶段写，每个
 * bot 都会写一条，未抢锁的那些就留下永不完成的孤儿行。故 save 闭包化、由接线点
 * 抢锁后调用；common_agent_response 的仓储逻辑仍只在本文件一处（闭包内），不
 * 泄漏到接线点。"有 pending 行 ⇔ 已发 MQ"不是系统不变量（消费方按 session_id /
 * trigger_message_id 查，只有 chat.response 真回来才命中，而那必在 publish
 * 之后），所以 save 时序后移是安全的。
 */
export async function makeTextReply(message: RuleMessage, ctx?: RuleHandlerContext): Promise<void> {
    const sessionId = uuidv4();
    const botName = context.getBotName() || undefined;

    const lane = context.getLane() || getLane() || undefined;
    const payload = buildChatRequestPayload(message, sessionId, botName, lane);

    // common_agent_response pending 行落库闭包：仓储逻辑只在此一处，但**不在此
    // 执行**。由渠道侧接线点抢到去重锁后调用，与 publish 紧邻。落库失败只记可查
    // 日志、不抛 —— pending 行是观测便利，不是发 MQ 的前置不变量。
    const savePending = async (): Promise<void> => {
        try {
            const repo = commonAgentResponseRepo();
            const agentResponse = repo.create({
                response_id: uuidv7(),
                session_id: sessionId,
                trigger_common_message_id: message.commonMessageId,
                common_conversation_id: message.commonConversationId,
                bot_name: botName,
                status: 'pending',
            } as Partial<CommonAgentResponse>);
            await repo.save(agentResponse);
        } catch (e) {
            console.error('Failed to create common_agent_response:', e);
        }
    };

    // 登记待发意图。dedupeKey 用 common_message_id（跨渠道唯一）；取锁、pending
    // 落库、publish 由渠道侧接线点在该渠道入站消息写入成功后紧邻执行。ctx 缺失
    // （理论上不会，接线点必传）则不登记 —— 防御性健壮，绝不退回旧的"handler
    // 内直接 publish"。
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
