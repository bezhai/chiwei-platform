import type { RuleMessage } from '@core/rules/rule-message';
import type { ChatRequestEnrichment } from '@core/services/ai/reply';
import { larkContextStore } from './lark-context-store';

// 飞书侧 chat.request 富化（B2 从 core/services/ai/reply.ts 的 channelContext
// 读取搬进 plugins/lark）。is_canary / mentions 是飞书专属：从 lark 私有 store
// 按 commonMessageId 取回飞书 Message，读 permission_config.is_canary 和
// getBotAppIds()。非飞书 channel 取中性默认（不泄漏飞书绑定）。
export function enrichLarkChatRequest(message: RuleMessage): ChatRequestEnrichment {
    if (message.channel !== 'lark') {
        return { isCanary: false, mentions: [] };
    }
    const lark = larkContextStore.get(message);
    return {
        isCanary: lark.basicChatInfo?.permission_config?.is_canary ?? false,
        mentions: lark.getBotAppIds(),
    };
}
