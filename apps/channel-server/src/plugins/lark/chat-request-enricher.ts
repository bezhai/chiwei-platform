import type { RuleMessage } from '@core/rules/rule-message';
import type { ChatRequestEnrichment } from '@core/services/ai/reply';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { larkContextStore } from './lark-context-store';

// 飞书侧 chat.request 富化。Lark app_id 只在插件层解析成 persona_id；
// agent-service 不再读取 bot_config.credentials 或理解 Lark mention。
export function enrichLarkChatRequest(message: RuleMessage): ChatRequestEnrichment {
    if (message.channel !== 'lark') {
        return { isCanary: false, personaIds: [] };
    }
    const lark = larkContextStore.get(message);
    const personaIds = lark.getBotAppIds()
        .map((appId) => multiBotManager.getBotConfigByAppId(appId)?.persona_id)
        .filter((personaId): personaId is string => Boolean(personaId));
    return {
        isCanary: lark.basicChatInfo?.permission_config?.is_canary ?? false,
        personaIds: Array.from(new Set(personaIds)),
    };
}
