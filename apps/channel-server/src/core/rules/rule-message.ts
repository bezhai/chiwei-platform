// RuleMessage —— runRules 消费的平台无关统一视图（决策五）。
//
// 一条消息进 runRules 前由 adapter 产出的 InboundMessage 派生成 RuleMessage：
// 平台无关部分（is_direct / 文本工具 / 结构化 mentions / createTime / 媒体类型
// 判断 / 全局 internal_*_id / channel）足以支撑 runRules 里真正平台无关的规则
// （persona 文本主链路 makeTextReply、EqualText、RegexpMatch、OnlyGroup、文本
// 限定等）。
//
// 飞书 ORM/SDK 强绑的东西（basicChatInfo / permission_config /
// senderInfo.is_admin / mentionMap 等）**不进平台无关契约**——它们封装成
// LarkRuleContext，经可选 channelContext 旁挂。lark-only handler/rule 经
// channel 过滤后从 channelContext 取 LarkRuleContext，缺 context 直接
// fail-loud（绝不静默降级、绝不静默跳过）。
//
// 飞书侧 RuleMessage 由 Lark 的 Message 富对象派生（buildLarkRuleMessage），
// 平台无关字段全部委托给 Message 现有等价方法 —— 飞书逐场景行为零变化：
// runRules 看到的 is_direct/clearText/mentions 等与改造前 Message 上读到的
// 完全一致，只是入参形态从 Message 变成 RuleMessage。

import type { Message } from 'core/models/message';

// 飞书强绑上下文：lark-only rule/handler 内部仍按现有逻辑读飞书 Message
// （basicChatInfo / senderInfo / mentionMap / hasMention 等）。这些字段平台
// 无关契约不承载，只在 LarkRuleContext 内旁挂。channel 字段用于 fail-loud
// 校验：channelContext 必须确实属于 'lark'，否则说明装配错了，宁可炸。
export interface LarkRuleContext {
    channel: 'lark';
    larkMessage: Message;
}

// 平台无关统一视图。文本/媒体工具是函数而非字段，与 Message 现有访问器
// 一一对偶（飞书侧直接委托 Message，QQ 等新 channel 按自身内容构造）。
export interface RuleMessage {
    channel: string;
    botName: string;

    // 全局 internal_*_id（IdentityResolver.resolve 之后的，不是 channel 裸 ID）。
    internalUserId: string;
    internalChatId: string;
    internalMessageId: string;
    internalRootId: string | undefined;

    // 派生自 InboundMessage.conversation_scope（飞书 p2p → direct → isDirect）。
    isDirect: boolean;

    // 结构化寻址线索（飞书是 mention 的 union_id 列表；QQ 是 at 的 appid）。
    addressedTargetIds: string[];

    // 派生自 received_at。
    createTime: number;

    // 文本工具（与 Message.clearText/text/withMentionText/withoutEmojiText 对偶）。
    clearText(): string;
    text(): string;
    withMentionText(): string;
    withoutEmojiText(): string;

    // 媒体类型判断（与 Message.isTextOnly/isStickerOnly/stickerKey/imageKeys 对偶）。
    isTextOnly(): boolean;
    isStickerOnly(): boolean;
    stickerKey(): string;
    imageKeys(): string[];

    // 飞书强绑上下文旁挂点。非飞书 channel 为 undefined。
    channelContext?: LarkRuleContext;
}

// lark-only rule/handler 的 fail-loud 取 context 入口。缺 channelContext 或
// channelContext 不属于 lark = 装配/过滤出错，绝不静默吞消息：在边界炸出来
// （与 channel-registry fail-closed、enforceDecision 同一取向）。
export function requireLarkContext(m: RuleMessage): LarkRuleContext {
    const ctx = m.channelContext;
    if (!ctx) {
        throw new Error(
            `lark-only rule/handler invoked but RuleMessage carries no channelContext ` +
                `(channel=${m.channel}, message=${m.internalMessageId}); ` +
                `fail-loud — silent skip/degrade is forbidden`,
        );
    }
    if (ctx.channel !== 'lark') {
        throw new Error(
            `lark-only rule/handler got channelContext for channel "${ctx.channel}" ` +
                `(expected "lark", message=${m.internalMessageId}); fail-loud`,
        );
    }
    return ctx;
}

// 飞书 Message 富对象 → 平台无关 RuleMessage。平台无关字段全部委托 Message
// 现有等价方法（行为零变化）；飞书强绑能力经 channelContext 旁挂，供 lark-only
// rule/handler 用 requireLarkContext 取回原 Message 跑不变的内部逻辑。
//
// 全局 internal_*_id 由调用方（接线点）经 IdentityResolver.resolve 后传入；
// buildLarkRuleMessage 不碰 DB / resolver，纯派生。
export function buildLarkRuleMessage(
    larkMessage: Message,
    ids: {
        botName: string;
        internalUserId: string;
        internalChatId: string;
        internalMessageId: string;
        internalRootId: string | undefined;
        addressedTargetIds: string[];
    },
): RuleMessage {
    return {
        channel: 'lark',
        botName: ids.botName,
        internalUserId: ids.internalUserId,
        internalChatId: ids.internalChatId,
        internalMessageId: ids.internalMessageId,
        internalRootId: ids.internalRootId,
        isDirect: larkMessage.isP2P(),
        addressedTargetIds: ids.addressedTargetIds,
        createTime: Number(larkMessage.createTime) || 0,
        clearText: () => larkMessage.clearText(),
        text: () => larkMessage.text(),
        withMentionText: () => larkMessage.withMentionText(),
        withoutEmojiText: () => larkMessage.withoutEmojiText(),
        isTextOnly: () => larkMessage.isTextOnly(),
        isStickerOnly: () => larkMessage.isStickerOnly(),
        stickerKey: () => larkMessage.stickerKey(),
        imageKeys: () => larkMessage.imageKeys(),
        channelContext: { channel: 'lark', larkMessage },
    };
}
