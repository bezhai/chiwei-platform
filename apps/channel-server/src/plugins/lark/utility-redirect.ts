import { replyMessage } from '@lark/basic/message';
import type { RuleMessage } from '@core/rules/rule-message';
import { larkContextStore } from './lark-context-store';

const TOOL_BOT_APPLY_URL = process.env.TOOL_BOT_APPLY_URL || '';

// 飞书侧 utility-redirect 引导提示（B2 从 engine.ts 搬进 plugins/lark）。
// persona bot 被 @ 却命中 utility 指令时，发飞书回复引导用户申请工具人 bot。
// engine 不认识飞书 SDK：它只调注入的 responder；本函数从 lark 私有 store
// 按 internalMessageId 取回飞书 Message 发回复（行为与改造前逐字一致）。
export function sendLarkUtilityRedirect(message: RuleMessage): void {
    const applyHint = TOOL_BOT_APPLY_URL
        ? `，请点击 ${TOOL_BOT_APPLY_URL} 申请将工具人添加到群聊`
        : '';
    const lark = larkContextStore.get(message.internalMessageId);
    replyMessage(lark.messageId, `工具类功能已迁移至「赤尾工具人」${applyHint}`, true);
}
