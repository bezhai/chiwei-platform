// 一条飞书消息进来之后，本服务按什么顺序做事。
//
//     解析（三个入口共用一份）
//        └─▶ 投影：换成公共层 id、落账      ← 交给别的泳道就在这里到此为止
//               ├─▶ 附件：把图片和文件交给 tool-service 存档（旁路，不等它）
//               └─▶ 规则：要不要开口，要就把请求发给 agent-service
//
// 三步都是注入的：这个文件只负责**顺序**，以及"上一步没成，下一步就不跑"。
//
// ## 这个顺序与拆分前不同（有意）
//
// 拆分前规则跑在写库**之前**：落库失败时用户已经看到了 utility 指令的回复，库里却
// 没有这条消息。本服务的形状不一样 —— 落账在投影内部就完成了，规则接在它之后，所以
// 落账失败则规则根本不跑，用户什么也看不到。这更正确：一条没能留下记录的消息，赤尾
// 也不该记得自己回过它。
//
// 投影抛错**照常往外抛**：泳道交接靠它应答非 2xx，飞书那两个入口早已 ACK、只能留下
// 一条可查错误（见 ingress/event-sink.ts）。在这里吞掉等于既没落库也没有任何信号。

import type { LarkEvent } from './ingress/lark-event';
import type { LarkMessageReading } from './message/read-message-event';
import type { LarkInboundOutcome, LarkRecordedInbound } from './projection/inbound-projection';

export interface LarkReceiveDeps {
    /** 换成公共层 id 并落账。这条该走别的泳道时交出去，返回 handed-off。 */
    project: (reading: LarkMessageReading, event: LarkEvent) => Promise<LarkInboundOutcome>;
    /**
     * 把这条消息带的图片和文件交给 tool-service 存档（见 attachments.ts）。
     *
     * **旁路，而且不等它。** 签名是 `void` 不是 `Promise`：入站绝不为一次 tool-service
     * 往返等待。gate（群没开"所有人可下载"）在里面，因为它要投影顺路读到的群资料。
     */
    cacheAttachments: (
        reading: LarkMessageReading,
        recorded: LarkRecordedInbound,
        event: LarkEvent,
    ) => void;
    /**
     * 落账之后：跑规则，看这条消息命中哪条飞书指令。
     *
     * 收的是投影**整份**产出而不只是那组公共层 id —— 指令层要用的飞书事实（is_admin、
     * 会话开关、群资料）是投影顺路读出来的，在这里被截掉的话，指令就只能各自再查一遍。
     */
    applyRules: (
        reading: LarkMessageReading,
        recorded: LarkRecordedInbound,
        event: LarkEvent,
    ) => Promise<void>;
}

export async function receiveLarkMessage(
    deps: LarkReceiveDeps,
    reading: LarkMessageReading,
    event: LarkEvent,
): Promise<void> {
    const outcome = await deps.project(reading, event);
    // 交出去的那一支投影内部已经打过日志了，这里不重复说一遍。
    if (outcome.kind !== 'recorded') return;

    console.info(
        `[lark-inbound] recorded ${reading.message.messageType} message ` +
            `${reading.message.messageId} as ${outcome.projection.commonMessageId} ` +
            `in conversation ${outcome.projection.commonConversationId}`,
    );

    // 附件缓存是旁路：**它坏掉绝不能让一条消息处理不下去**。里面已经逐条吞错了，这里
    // 再兜一层是因为契约由本文件持有 —— 换一份实现进来也不该有机会拖垮入站。
    try {
        deps.cacheAttachments(reading, outcome, event);
    } catch (error) {
        console.error('[lark-inbound] could not hand the attachments to tool-service:', error);
    }

    await deps.applyRules(reading, outcome, event);
}
