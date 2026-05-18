// 5b 入站接线核心（channel 无关）。把 InboundAdapter / AddressingPolicy /
// IdentityResolver 三个契约按**钉死的链路顺序**串起来，产出"是否响应 + 全局
// internal_*_id"，供 handlers.ts 接进真实链路（runRules / storeMessage / MQ）。
//
// 钉死顺序（决策五 / spec「整体分层」，不可调换）：
//   adapter.parse(raw)
//     → AddressingPolicy.decide + enforceDecision（前置总闸：不依赖 DB 的
//       纯判定，即使后面 resolve 的 DB 挂了，这一步也已先产出"为什么不回"
//       的可查结论；reason 空 = 静默丢弃 = enforceDecision 抛错）
//     → IdentityResolver.resolve（channel 裸 ID → 全局 internal_*_id；后续
//       runRules/storeMessage/MQ 全靠它）
//
// fail-loud 铁律（spec 5b）：契约链(parse/decide/resolve)任一失败 = 该消息
// fail-loud —— 本函数返回 ok=false，调用方据此**不写库、不发 MQ、记可查
// 错误日志，绝不退回 channel 裸 ID 往下走**。binding/全局 ID 缺失 = 失败 =
// fail-loud。杜绝旧实现"退裸 ID → worker 无脑反查 → 异步炸"那条路。
//
// 关于"前置总闸 respond=false 不进 runRules"与飞书复读的取舍：见 handlers.ts
// 接线注释 —— respond 仅 gate persona 主链路；飞书 native 链路（复读用
// NeedNotRobotMention、storeMessage、识图）对非 @bot 群消息必须照常运行，否
// 则是飞书行为回归（违反"飞书逐场景零变化"硬约束）。故本函数对 parse 非 null
// 的消息无论 respond 与否都翻译全局 ID（全局 ID 是写入点数据来源），respond
// 只作为调用方决定要不要进 persona 响应流程的判定结果一并带出。

import {
    enforceDecision,
    type AddressingDecision,
    type InboundMessage,
} from './contracts';
import type { IdentityResolver } from './identity-resolver';

export interface InboundContractChainInput {
    // 该 channel 的原始入站事件（飞书是 LarkReceiveMessage；契约层不感知形状）。
    params: unknown;
    // adapter.parse 的纯转换（同步，零 I/O）。
    parse: (params: unknown) => InboundMessage | null;
    // AddressingPolicy.decide。
    decide: (msg: InboundMessage, botIdentity: string) => AddressingDecision;
    // 调用方按 channel 取的 bot 标识（飞书=robot_union_id），口径须与该
    // channel adapter 的 AddressingHint.targetId 同源。
    botIdentity: string;
    resolver: IdentityResolver;
    // respond=false 时的可查日志落点（enforceDecision 保证 reason 非空）。
    logSkip: (reason: string) => void;
}

export type InboundContractChainResult =
    | {
          ok: true;
          // AddressingPolicy 总判定结果。respond=false 已记可查日志（非静默）。
          respond: boolean;
          // 全局 internal_*_id（resolve 之后）。parse 非 null 必产出。
          globalUserId: string;
          globalChatId: string;
          globalMessageId: string;
          globalRootId: string | undefined;
          // 原始解析出的平台无关消息（供调用方派生 RuleMessage）。
          inbound: InboundMessage;
      }
    | {
          ok: false;
          // parsed_null：adapter 判定这不是要处理的消息（平台杂事件）。
          // contract_chain_error：parse/decide/enforce/resolve 任一抛错 →
          //   fail-loud，调用方不写库不发 MQ 不退裸 ID。
          reason: 'parsed_null' | 'contract_chain_error';
          detail?: string;
      };

export async function runInboundContractChain(
    input: InboundContractChainInput,
): Promise<InboundContractChainResult> {
    try {
        // 1. parse（纯转换）。null = 平台杂事件，非错误，调用方跳过契约链。
        const msg = input.parse(input.params);
        if (msg === null) {
            return { ok: false, reason: 'parsed_null' };
        }

        // 2. 前置总闸：decide + enforceDecision。必须前置于 resolve——它是
        //    不依赖 DB 的纯判定，即使后面 resolve 的 DB 挂了，这一步也已
        //    先产出"为什么不回"。enforceDecision：respond=false 且 reason
        //    空 → 抛错（静默丢弃在边界炸掉，不无声吞消息）。
        const decision = input.decide(msg, input.botIdentity);
        const respond = enforceDecision(decision, input.logSkip);

        // 3. IdentityResolver.resolve：channel 裸 ID → 全局 internal_*_id。
        //    user/chat/message 必翻；root 若有也翻。任一抛错 → fail-loud。
        const globalUserId = await input.resolver.resolve(
            'user',
            msg.channel,
            msg.channel_user_id,
        );
        const globalChatId = await input.resolver.resolve(
            'chat',
            msg.channel,
            msg.channel_chat_id,
        );
        const globalMessageId = await input.resolver.resolve(
            'message',
            msg.channel,
            msg.channel_message_id,
        );
        let globalRootId: string | undefined;
        const rootChannelMsgId = msg.thread_ref?.rootChannelMessageId;
        if (rootChannelMsgId) {
            globalRootId = await input.resolver.resolve(
                'message',
                msg.channel,
                rootChannelMsgId,
            );
        }

        return {
            ok: true,
            respond,
            globalUserId,
            globalChatId,
            globalMessageId,
            globalRootId,
            inbound: msg,
        };
    } catch (e) {
        // fail-loud：parse/decide/enforce/resolve 任一失败收敛到这里。
        // 不产出 globalIds，调用方据此不写库不发 MQ、记错误日志，绝不
        // 退回 channel 裸 ID。
        return {
            ok: false,
            reason: 'contract_chain_error',
            detail: e instanceof Error ? e.message : 'unknown contract chain error',
        };
    }
}
