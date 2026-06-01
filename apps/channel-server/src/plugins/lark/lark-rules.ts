import type { LarkBaseChatInfo } from 'infrastructure/dal/entities';
import type { RuleMessage } from '@core/rules/rule-message';
import { larkContextStore } from './lark-context-store';

// 飞书强绑谓词 —— B2 从 core/rules/rule.ts 搬进 plugins/lark（它们读飞书专属的
// senderInfo.is_admin / basicChatInfo.permission_config，归属飞书插件）。
//
// 与改造前差别只在「飞书数据从哪来」：不再经 requireLarkContext 掏旁挂的
// larkMessage，而是用平台无关 RuleMessage 的 commonMessageId 从 lark 私有
// store 取回飞书 Message。判定逻辑逐字一致；缺 store entry fail-loud（绝不静默）。

type Rule = (message: RuleMessage) => boolean;

export const WhiteGroupCheck =
    (checkFunc: (chatInfo: LarkBaseChatInfo) => boolean): Rule =>
    (message) => {
        const lark = larkContextStore.get(message);
        const chatInfo = lark.basicChatInfo;
        return chatInfo ? checkFunc(chatInfo) : false;
    };

export const IsAdmin: Rule = (message) => {
    const lark = larkContextStore.get(message);
    return lark.senderInfo?.is_admin ?? false;
};
