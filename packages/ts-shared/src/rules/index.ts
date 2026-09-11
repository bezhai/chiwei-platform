// 规则引擎。渠道无关：规则只消费 RuleMessage（各渠道插件在自己那侧把渠道裸 id
// 收敛成 common id 之后构造出来的平台无关视图），引擎本身不认识任何具体渠道的
// 指令 —— 指令归属靠注册（CommandRegistry），不靠 flag。

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
    RuleEngineDeps,
    RuleTerminalKind,
    RuleTerminalState,
} from './engine';
export { runRules, runRulesWith } from './engine';

export { CommandRegistry, getCommandRegistry } from './command-registry';
