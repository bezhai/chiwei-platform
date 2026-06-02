import { UserBlacklistRepository } from '@infrastructure/dal/repositories/repositories';
import { type RuleMessage } from './rule-message';
import type { RuleHandlerContext } from './engine';

// 规则/处理器一律消费平台无关 RuleMessage（决策五）。本文件只保留**真正平台
// 无关**的规则（EqualText/RegexpMatch/OnlyGroup/文本限定/NeedRobotMention/
// NotBlocked 等），直接读 RuleMessage 的平台无关视图。
//
// 飞书强绑规则（WhiteGroupCheck/IsAdmin）已搬进 plugins/lark/lark-rules.ts
// （B2）：它们读飞书专属字段、从 lark 私有 store 取飞书数据，不属于 core。

type Rule = (message: RuleMessage) => boolean;

type AsyncRule = (message: RuleMessage) => Promise<boolean>;

// handler 第二参 ctx 可选（决策一）：persona 文本主链路用
// ctx.registerPendingChatTrigger 把待发 ChatTrigger 意图回传引擎，由接线点
// 在 common/lark 入站消息写入成功后再发 MQ。其余 handler 不声明此参即可。
type Handler = (
    message: RuleMessage,
    ctx?: RuleHandlerContext,
) => Promise<void>;

/** 规则分类：utility=工具功能, persona=拟人聊天 */
export type RuleCategory = 'utility' | 'persona';

// 定义规则和对应处理逻辑的结构。新增 channels 渠道声明字段（决策五范围收紧）：
//   - 不声明 = 默认全平台（只有 persona 文本主链路这样，真正平台无关）。
//   - 声明 ['lark'] = 仅飞书：runRules 按消息 channel 过滤，非飞书消息跳过
//     （并入终态记录的 skipped）。凡 import 飞书 SDK/card/实体或读飞书专属
//     字段的 chatRule 必须显式声明 channels:['lark']。
export interface RuleConfig {
    rules: Rule[];
    async_rules?: AsyncRule[];
    handler: Handler;
    fallthrough?: boolean;
    comment?: string;
    category?: RuleCategory;
    channels?: string[];
}

// ---- 平台无关规则（直接读 RuleMessage 平台无关视图）----

// 与现有 NeedRobotMention 逻辑等价：私聊直通，群聊必须 @ 当前 bot。区别是
// 这里完全用 common identity：botCommonUserId 是当前 bot 在 common_user 里的
// 身份，mentionedUserIds 是消息里所有可识别 mention 投影后的 common_user_id
// 列表。飞书 open_id/union_id、QQ appid 等平台裸 id 必须在插件层换完，core 不知道。
export function NeedRobotMention(message: RuleMessage): boolean {
    if (message.isDirect) return true;
    return message.mentionedUserIds.includes(message.botCommonUserId);
}

export function NeedNotRobotMention(message: RuleMessage): boolean {
    return !NeedRobotMention(message);
}

export function TextMessageLimit(message: RuleMessage): boolean {
    return message.isTextOnly();
}

export function ContainKeyword(keyword: string): Rule {
    return (message) => message.text().includes(keyword);
}

export function EqualText(...texts: string[]): Rule {
    return (message) => texts.some((text) => message.clearText() === text);
}

export function RegexpMatch(pattern: string): Rule {
    return (message) => {
        try {
            return new RegExp(pattern).test(message.clearText());
        } catch {
            return false;
        }
    };
}

export function OnlyP2P(message: RuleMessage): boolean {
    return message.isDirect;
}

export function OnlyGroup(message: RuleMessage): boolean {
    return !message.isDirect;
}

// 异步规则：检查用户是否未被拉黑。黑名单表当前列名仍为 union_id，但值口径
// 已收敛为 common_user_id；后续只需要单独改列名，不再依赖飞书 union_id。
export async function NotBlocked(message: RuleMessage): Promise<boolean> {
    const globalUserId = message.commonUserId;
    if (!globalUserId || globalUserId === 'unknown_sender') return true;

    const blocked = await UserBlacklistRepository.findOne({
        where: { union_id: globalUserId },
    });
    return !blocked;
}
