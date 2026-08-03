// 规则引擎与聊天主链路。渠道无关：规则只消费 RuleMessage（各渠道插件在自己
// 那侧把渠道裸 id 收敛成 common id 之后构造出来的平台无关视图），引擎本身不
// 认识任何具体渠道的指令 —— 指令归属靠注册（CommandRegistry），不靠 flag。

export type { RuleMessage } from './rule-message';

export type { RuleCategory, RuleConfig } from './rule';
export {
    ContainKeyword,
    EqualText,
    NeedNotRobotMention,
    NeedRobotMention,
    NotBlocked,
    OnlyGroup,
    OnlyP2P,
    RegexpMatch,
    TextMessageLimit,
} from './rule';

export type {
    PendingChatTrigger,
    RuleEngineDeps,
    RuleHandlerContext,
    RuleTerminalKind,
    RuleTerminalState,
} from './engine';
export { runRules, runRulesWith } from './engine';

export type {
    ChatRequestEnricher,
    ChatRequestEnrichment,
    ChatRequestPayload,
} from './chat-request';
export {
    buildChatRequestPayload,
    makeTextReply,
    registerChatRequestEnricher,
    resetChatRequestEnrichers,
} from './chat-request';

export { CommandRegistry, getCommandRegistry } from './command-registry';
