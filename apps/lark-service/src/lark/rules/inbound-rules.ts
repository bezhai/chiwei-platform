// 投影完成之后：这条消息要不要让赤尾开口，要的话把请求交给 agent-service。
//
//     RuleMessage ──▶ runRulesWith ──▶ RuleTerminalState
//                                          │
//                         带待发意图 ──────┴──▶ 去重锁 → 认领 bot → 落 pending 行 → publish
//
// ## 规则序列是参数，不是进程级注册表
//
// 共享包有两个入口：`runRules` 从进程级单例 CommandRegistry 取规则，`runRulesWith`
// 的规则从参数进。这里用后者。理由不是"更好测"，是前者对本服务**没有意义**：那个
// 注册表按 channel 分发，而本服务只有一个渠道；注册又发生在 import 期（模块加载
// 副作用），谁进了模块图决定了跑哪些规则，测试之间还会累积。
//
// 代价是 `runRules` 顺手装配的两件事要自己接上：**botRole**（从 bot 目录取，决定这个
// bot 认不认某一类规则）和 **notBlocked**（黑名单）。两者都在 deps 里。
//
// ## 顺序被专门排过，不能动
//
// 取去重锁、认领 bot、落 pending 行、publish 四者必须紧邻，且就是这个顺序：
//
//   * **锁在消息落库之后才取**（落库在投影里就完成了，本模块跑在它之后）。反过来的话
//     落库失败会让这把锁空占着，这条消息在它过期之前没有任何 bot 能再处理。
//   * **pending 行在抢到锁之后才写**。没抢到的 bot 不 publish，它要是也写了 pending
//     行，那就是一条永不完成的孤儿行 —— 真正会有回复的是抢到锁那个 bot 的 session。
//
// 共享包的 makeTextReply 之所以只登记意图、不自己 publish，就是为了把这个顺序交给
// 渠道侧掌握。
//
// ## 半路失败必须把锁还回去，否则消息被吃掉
//
// `make_reply:<commonMessageId>` 这个标记同时背着两个意思：**「有人正在处理」**和
// **「已经发出去了」**。抢到之后半路失败（认领炸了、broker 挂了）的那条流留下的是
// 前者，而读它的人当成后者 —— 于是泳道那条路 requeue 立即重投，重投时抢不到、走进
// "别人已经发过了"分支、**正常返回并被 ACK**，这条消息就此消失，连一条错误都不剩。
//
// 所以失败路径上把锁还回去（比对持有者再删，理由同 projection/message-lock.ts 那段
// Lua：标记有租期，这中间可能已经易主）。成功路径不退 —— 那时它表达的确实是"已经发
// 出去了"，还了等于把去重让出去，同群另一个 bot 会再发一遍。
//
// 代价是失败若发生在 savePending 之后，重投会再写一条 pending 行、留下一条孤儿。这是
// 可接受的：pending 行是观测便利，"有 pending 行 ⇔ 已发 MQ"本来就不是系统不变量
// （消费方按 session_id / trigger_message_id 查，只有真回复回来才命中）。拿一条多余的
// 观测行换"消息不会被静默吃掉"，是划算的。
//
// ## 这一段与拆分前的两处差别（都是有意的）
//
// **规则跑在落库之后。** 拆分前 runRules 在 storeLarkInboundMessage 之前，落库失败时
// 用户已经看到了 utility 指令的回复、库里却没有这条消息。本服务的形状不同：落库在
// projectLarkInbound 内部就完成了，规则接在它之后，落库失败则规则根本不跑（见
// receive-message.ts）。新形状更正确，采用它。
//
// **om_id 锁的覆盖范围。** 拆分前把 runRules、落库、去重锁、publish 全包在按 om_id
// 取的那把锁里。本服务的 om_id 锁只包住投影（见 projection/inbound-projection.ts），
// 规则段不在锁里。"同一条群消息只 publish 一次"这个不变量不受影响 —— 保证它的从来
// 不是 om_id 锁，而是 `make_reply:<commonMessageId>` 这把独立的去重锁（inbound-rules
// .test.ts 的「多个 bot，一条消息」用两条无锁并发的流钉住了这一点）。

import type { BotConfig } from '@inner/shared/entities';
import { CHAT_REQUEST, type Route } from '@inner/shared/mq';
import {
    NeedRobotMention,
    makeTextReply,
    registerChatRequestEnricher,
    runRulesWith,
} from '@inner/shared/rules';
import type {
    ChatRequestPayload,
    RuleConfig,
    RuleMessage,
    RuleTerminalState,
} from '@inner/shared/rules';

import { larkPersonaIdOf, type LarkBotRoster } from '../bot-lookup';
import { LARK_CHANNEL } from '../channel';
import type { LarkEvent } from '../ingress/lark-event';
import type { LarkMessageReading } from '../message/read-message-event';
import type { LarkInboundProjection } from '../projection/inbound-projection';
import type { CommonMessageClaim } from '../projection/tables';
import { larkChatRequestEnricher } from './chat-request';
import { larkRuleMessage } from './rule-message';

/**
 * 本服务认的规则序列。
 *
 * 现在只有共享包那条聊天主链路（真正渠道无关的那一条）。飞书自己的斜杠指令还在
 * channel-server 里，跟着 Task D 一起搬过来 —— 到时候加在**这条之前**，聊天主链路是
 * `NeedRobotMention` 的兜底。
 */
export const LARK_CHAT_RULES: RuleConfig[] = [
    {
        rules: [NeedRobotMention],
        handler: makeTextReply,
        comment: '聊天',
        category: 'persona',
    },
];

/** 这条消息归谁处理的认领。 */
export interface LarkMessageClaim {
    commonMessageId: string;
    botName: string;
    commonUserId: string;
}

export interface LarkRulesDeps {
    /** 这个进程认哪些规则。装配期传入（见文件顶部）。 */
    chatRules: RuleConfig[];
    /** 这个 bot 是人设还是工具。取不到就是不过滤。 */
    botRoleOf: (botName: string) => string | undefined;
    /** 这个 bot 在 common_user 里的身份。取不到直接抛 —— 群聊寻址全靠它。 */
    botCommonUserId: (botName: string) => string;
    notBlocked: (message: RuleMessage) => Promise<boolean>;
    /**
     * 取去重锁（"由我来发这条消息的请求"）。抢到返回持有者 token，没抢到返回 null。
     */
    claimChatTrigger: (dedupeKey: string) => Promise<string | null>;
    /**
     * 把锁还回去。**只删自己那把** —— 锁有租期，这中间它可能已经过期并易主，
     * 无条件删就是把别人正在用的去重删掉。
     */
    releaseChatTrigger: (dedupeKey: string, token: string) => Promise<void>;
    claimMessageForBot: (claim: LarkMessageClaim) => Promise<void>;
    publishChatRequest: (payload: ChatRequestPayload, lane: string | undefined) => Promise<void>;
}

/**
 * 跑规则，并把规则登记的那个待发意图（如果有）完成掉。
 *
 * 规则引擎从不向调用方抛错：所有退出路径都收敛成 RuleTerminalState，所以这里按终态
 * 分支，不需要也不该在规则那一段套 try/catch。返回终态是给调用方看的（可观测），
 * 不是给它做决定的。
 *
 * 认领与 publish 失败**先把锁还回去再往上抛**（见文件顶部）：泳道那条路会重投，
 * 重投时才能真的重来；飞书那两个入口早就 ACK 过，抛出去只留一条错误日志。
 */
export async function applyLarkRules(
    deps: LarkRulesDeps,
    reading: LarkMessageReading,
    projection: LarkInboundProjection,
    event: LarkEvent,
): Promise<RuleTerminalState> {
    const message = larkRuleMessage(reading, projection, {
        botName: event.botName,
        commonUserId: deps.botCommonUserId(event.botName),
    });

    const terminal = await runRulesWith(message, {
        chatRules: deps.chatRules,
        botRole: deps.botRoleOf(event.botName),
        notBlocked: deps.notBlocked,
    });
    logTerminalState(terminal);

    const pending = terminal.pendingChatTrigger;
    if (!pending) return terminal;

    // ---- 这四步紧邻，顺序见文件顶部 ----
    const token = await deps.claimChatTrigger(pending.dedupeKey);
    if (token === null) {
        console.info(
            `[lark-rules] another bot already published this one: ` +
                `message=${projection.commonMessageId} bot=${event.botName}`,
        );
        return terminal;
    }

    try {
        await deps.claimMessageForBot({
            commonMessageId: projection.commonMessageId,
            botName: event.botName,
            commonUserId: projection.commonUserId,
        });
        await pending.savePending();
        await deps.publishChatRequest(pending.payload, pending.lane);
    } catch (error) {
        // 半路失败必须把锁还回去，理由见文件顶部。还完照常往上抛。
        await handBackQuietly(deps, pending.dedupeKey, token, projection.commonMessageId);
        throw error;
    }

    console.info(
        `[lark-rules] published chat.request: session=${pending.payload.session_id} ` +
            `message=${projection.commonMessageId} lane=${pending.lane || 'prod'}`,
    );
    return terminal;
}

/**
 * 还锁失败只记日志，绝不盖掉原始错误 —— 调用方要看到的是"为什么没发出去"。
 *
 * 还不回去的后果要说清楚：这条消息在锁过期之前重投也发不出来（会走进"别人已经
 * 发过了"分支被 ACK）。所以这条日志本身就是"有一条消息被吃掉了"的唯一线索。
 */
async function handBackQuietly(
    deps: LarkRulesDeps,
    dedupeKey: string,
    token: string,
    commonMessageId: string,
): Promise<void> {
    try {
        await deps.releaseChatTrigger(dedupeKey, token);
    } catch (releaseError) {
        console.error(
            `[lark-rules] could not hand back the chat.request claim for ` +
                `message=${commonMessageId}; it cannot be retried until the claim ` +
                `expires and will be silently dropped in the meantime:`,
            releaseError,
        );
    }
}

/**
 * 每条消息一条可查记录。共享包的 runRules 自带这一步，runRulesWith 不带（它是纯
 * 内核）—— 单一终态出口的价值全在"走哪条路都能查到"，所以换了入口就得自己补上。
 */
function logTerminalState(state: RuleTerminalState): void {
    const head =
        `[lark-rules] terminal=${state.kind} message=${state.messageId} ` +
        `chat=${state.chatId} user=${state.userId}`;
    const tail =
        (state.matchedRule ? ` matched="${state.matchedRule}"` : '') +
        (state.detail ? ` detail="${state.detail}"` : '') +
        (state.skipped.length > 0 ? ` skipped=[${state.skipped.join(' | ')}]` : '');
    if (state.kind === 'handler_error' || state.kind === 'rule_error') {
        console.error(head + tail);
    } else {
        console.info(head + tail);
    }
}

// ---------------------------------------------------------------------------
// 真实装配
// ---------------------------------------------------------------------------
//
// 端口在上面，把基础设施接上去的那几行在这里 —— 与 projection/message-lock.ts 同一个
// 形状（端口 + 真身在一个模块里，注入的只有更底层的东西）。
//
// 这几行放在组装根 index.ts 里也能跑，但那里**测不到**：index.ts 一 import 就要连
// PG / Redis / MQ。而这里错法都是静默的（漏注册富化 → 群聊全哑、漏接 lane → 泳道
// 消息发进 prod），一条错误日志都不会有。所以把它挪进来、基础设施收成窄端口。

/**
 * 去重锁的存取。**只要两个动作**，而且形状与 projection/message-lock.ts 的
 * MessageLockStore 完全一致 —— 生产上传进来的就是那份 Redis 实现，比对持有者再删的
 * 那段 Lua 全仓只写一次。
 */
export interface ChatTriggerMarkerStore {
    acquire(key: string, token: string, leaseSeconds: number): Promise<boolean>;
    release(key: string, token: string): Promise<void>;
}

/** MQ 里我们要用的那一个方法。位置参数照抄真身 —— lane 是第 5 个，接错就发进 prod。 */
export interface LarkRequestBroker {
    publish(
        route: Route,
        body: Record<string, unknown>,
        delayMs?: number,
        headers?: Record<string, unknown>,
        lane?: string,
    ): Promise<void>;
}

/** bot 目录里规则段要问的三件事。生产上是 botDirectory 本身。 */
export interface LarkBotFacts extends LarkBotRoster {
    getBotConfig(botName: string): BotConfig | null;
    getBotCommonUserId(botName: string): string;
}

export interface LarkRulesInfra {
    bots: LarkBotFacts;
    store: { claimCommonMessageForBot(claim: CommonMessageClaim): Promise<void> };
    marker: ChatTriggerMarkerStore;
    broker: LarkRequestBroker;
    notBlocked: (message: RuleMessage) => Promise<boolean>;
}

/**
 * 去重锁的租期。
 *
 * 走完之后它不再是互斥，而是"这条消息的请求已经发出去了"的标记，靠过期消失（半路
 * 失败的那条流会主动还锁，见文件顶部）。60s 要盖住的是同一条群消息在几个 bot 之间
 * 到达的时间差，不是处理时长。
 */
export const CHAT_TRIGGER_CLAIM_SECONDS = 60;

export function assembleLarkRules(infra: LarkRulesInfra): LarkRulesDeps {
    // 富化必须跟规则段**一起**装上。漏了不报错、不留日志：共享包
    // buildChatRequestPayload 找不到 lark 的 enricher 就悄悄退回中性默认，persona_ids
    // 恒空，而群聊 persona_ids 为空在 agent-service 那侧就是不回复 —— 赤尾在所有群里
    // 全哑。所以它不是"组装根顺手做的一件事"，是这份依赖成立的一部分。
    registerChatRequestEnricher(
        LARK_CHANNEL,
        larkChatRequestEnricher((commonUserId) => larkPersonaIdOf(infra.bots, commonUserId)),
    );

    return {
        chatRules: LARK_CHAT_RULES,
        // 这两件事拆分前由共享包的 runRules 顺手装配，换成可注入内核之后归这里。
        botRoleOf: (botName) => infra.bots.getBotConfig(botName)?.bot_role,
        botCommonUserId: (botName) => infra.bots.getBotCommonUserId(botName),
        notBlocked: infra.notBlocked,

        claimChatTrigger: async (dedupeKey) => {
            const token = Bun.randomUUIDv7();
            const won = await infra.marker.acquire(dedupeKey, token, CHAT_TRIGGER_CLAIM_SECONDS);
            return won ? token : null;
        },
        releaseChatTrigger: (dedupeKey, token) => infra.marker.release(dedupeKey, token),

        claimMessageForBot: (claim) =>
            infra.store.claimCommonMessageForBot({
                common_message_id: claim.commonMessageId,
                bot_name: claim.botName,
                common_user_id: claim.commonUserId,
            }),

        publishChatRequest: (payload, lane) =>
            infra.broker.publish(
                CHAT_REQUEST,
                payload as unknown as Record<string, unknown>,
                undefined,
                undefined,
                lane,
            ),
    };
}
