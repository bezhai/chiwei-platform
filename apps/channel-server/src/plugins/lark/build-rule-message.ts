import type { Message } from '@core/models/message';
import type { RuleMessage } from '@core/rules/rule-message';
import { larkContextStore } from './lark-context-store';

// 飞书 Message 富对象 → 平台无关 RuleMessage。B2 从 core/rules/rule-message.ts
// 搬进 lark 插件（飞书强绑，归属 plugins/lark）。
//
// 与改造前的关键差别：不再把 Message 旁挂到 RuleMessage.channelContext
// （那是 #228 的逃生口）。改成把 Message put 进 lark 私有 store（key=全局
// commonMessageId + botName），由 lark 谓词/handler 后续 get 取回。RuleMessage
// 保持纯平台无关视图：runRules 看到的是 is_direct、clearText 和 common mention
// ids，不再看到飞书 union_id/open_id；core 再也看不到飞书对象。
//
// common_* id 由调用方（接线点 handlers.ts）从 lark common projector 传入；
// 本函数不碰 DB，纯派生 + put store。
export function buildLarkRuleMessage(
    larkMessage: Message,
    ids: {
        botName: string;
        commonUserId: string;
        commonConversationId: string;
        commonMessageId: string;
        commonRootMessageId: string | undefined;
        botCommonUserId: string;
        mentionedUserIds: string[];
    },
): RuleMessage {
    // 飞书原始 Message 进 lark 私有 store，供 lark 谓词/handler 取回。
    larkContextStore.put(ids, larkMessage);

    return {
        channel: 'lark',
        botName: ids.botName,
        commonUserId: ids.commonUserId,
        commonConversationId: ids.commonConversationId,
        commonMessageId: ids.commonMessageId,
        commonRootMessageId: ids.commonRootMessageId,
        isDirect: larkMessage.isP2P(),
        botCommonUserId: ids.botCommonUserId,
        mentionedUserIds: ids.mentionedUserIds,
        createTime: Number(larkMessage.createTime) || 0,
        clearText: () => larkMessage.clearText(),
        text: () => larkMessage.text(),
        withMentionText: () => larkMessage.withMentionText(),
        withoutEmojiText: () => larkMessage.withoutEmojiText(),
        isTextOnly: () => larkMessage.isTextOnly(),
        isStickerOnly: () => larkMessage.isStickerOnly(),
        stickerKey: () => larkMessage.stickerKey(),
        imageKeys: () => larkMessage.imageKeys(),
    };
}
