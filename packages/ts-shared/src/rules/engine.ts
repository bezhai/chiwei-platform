import { RuleConfig, NotBlocked } from './rule';
import { context } from '../middleware/context';
import { botDirectory } from '../bot/bot-directory';
import { getCommandRegistry } from './command-registry';
import { type RuleMessage } from './rule-message';

// ---- 决策四：单一终态出口 ----
// 每一条进入 runRules 的消息，无论走哪条退出路径，都必须收敛到一个唯一、
// 明确、可查的 RuleTerminalState：要么记"响应了什么"，要么记"为什么没响应"。
// 禁止任何无终态记录的静默 break/return —— 所有退出点都必须 return 一个
// RuleTerminalState（这是函数返回值，类型系统强制覆盖每条路径）。
export type RuleTerminalKind =
    | 'blocked' // NotBlocked 黑名单挡掉
    | 'responded' // 命中某规则、handler 成功执行
    | 'handler_error' // 命中某规则、handler 抛异常（被捕获，仍记终态）
    | 'rule_error' // 规则执行阶段本身抛异常：notBlocked 调用 / sync 谓词 / async rule
    | 'no_match'; // 走完所有规则无任何匹配（含被 channel/botRole 过滤跳过）

export interface RuleTerminalState {
    kind: RuleTerminalKind;
    channel: string;
    messageId: string; // 全局 common_message_id
    chatId: string;
    userId: string;
    matchedRule?: string; // responded/handler_error 时命中的规则 comment
    detail?: string; // handler_error 时的错误信息 / blocked 原因
    // 走到 no_match 之前，被 channel 过滤 / botRole 不匹配 / 规则不通过而跳过
    // 的规则清单。每一条跳过都在此留痕——禁止任何静默跳过不留记录。
    skipped: string[];
}

// runRules 的可注入内核：依赖（chatRules / botRole / NotBlocked）全部从参数
// 进，不直接摸 bot 目录 / DB —— 单测纯跑、真实链路由 runRules 注入。
export interface RuleEngineDeps {
    chatRules: RuleConfig[];
    botRole: string | undefined;
    notBlocked: (m: RuleMessage) => Promise<boolean>;
}

function ruleLabel(cfg: RuleConfig, idx: number): string {
    return cfg.comment ? cfg.comment : `rule#${idx}`;
}

// 决策五：渠道声明 + channel 过滤。channels 未声明 = 默认全平台（只有真正
// 平台无关的 persona 文本主链路这样）；声明了则当前消息 channel 不在其中就
// 跳过这条指令（并入终态 skipped）。
function ruleSupportsChannel(cfg: RuleConfig, channel: string): boolean {
    if (cfg.channels === undefined) return true;
    return cfg.channels.includes(channel);
}

export async function runRulesWith(
    message: RuleMessage,
    deps: RuleEngineDeps,
): Promise<RuleTerminalState> {
    const base = {
        channel: message.channel,
        messageId: message.commonMessageId,
        chatId: message.commonConversationId,
        userId: message.commonUserId,
    };
    const skipped: string[] = [];
    // fallthrough 路径下"最后一次成功响应"的本地暂存（单一终态：循环结束
    // 统一收敛）。本地变量而非模块级——并发消息互不污染。
    let lastResponded: RuleTerminalState | undefined;

    // 退出路径 1：黑名单挡掉 —— 终态 blocked。
    // 退出路径 1b：黑名单检查本身抛错/reject —— 收敛终态 rule_error，不裸逃。
    let notBlocked: boolean;
    try {
        notBlocked = await deps.notBlocked(message);
    } catch (e) {
        return {
            ...base,
            kind: 'rule_error',
            matchedRule: 'notBlocked (blacklist check)',
            detail: e instanceof Error ? e.message : 'Unknown error',
            skipped,
        };
    }
    if (!notBlocked) {
        return {
            ...base,
            kind: 'blocked',
            detail: `user ${message.commonUserId} is blacklisted`,
            skipped,
        };
    }

    for (let idx = 0; idx < deps.chatRules.length; idx++) {
        const cfg = deps.chatRules[idx]!;
        const label = ruleLabel(cfg, idx);
        const { rules, handler, fallthrough, async_rules, category } = cfg;

        // 退出路径 2：channel 过滤跳过（决策五）。不静默——并入 skipped。
        if (!ruleSupportsChannel(cfg, message.channel)) {
            skipped.push(`${label} (channel ${message.channel} not in declared channels)`);
            continue;
        }

        // 退出路径 3b：sync 谓词 / async rule 执行本身抛错/reject ——
        // 收敛终态 rule_error（指明哪条规则），不裸逃出绕过 logTerminalState。
        let syncRulesPass: boolean;
        let asyncRulesPass: boolean;
        try {
            syncRulesPass = rules.every((rule) => rule(message));
            asyncRulesPass = async_rules
                ? (await Promise.all(async_rules.map((rule) => rule(message)))).every(
                      (result) => result,
                  )
                : true;
        } catch (e) {
            return {
                ...base,
                kind: 'rule_error',
                matchedRule: `${label} (rule predicate)`,
                detail: e instanceof Error ? e.message : 'Unknown error',
                skipped,
            };
        }

        // 退出路径 3：同步/异步规则不通过 —— 跳过该规则（留痕，非静默）。
        if (!(syncRulesPass && asyncRulesPass)) {
            skipped.push(`${label} (rules not satisfied)`);
            continue;
        }

        // 退出路径 4：botRole/category 不匹配。
        if (deps.botRole && category && category !== deps.botRole) {
            // botRole 与规则分类不一致时跳过（留痕）。persona bot 命中
            // utility 指令也不再回工具人提示，而是继续往后兜底到聊天主链路。
            skipped.push(`${label} (botRole=${deps.botRole} != category=${category})`);
            if (!fallthrough) {
                if (deps.botRole === 'persona' && category === 'utility') {
                    continue;
                }
                return { ...base, kind: 'no_match', skipped };
            }
            continue;
        }

        // 命中：执行 handler。
        // 退出路径 5（handler 抛异常）/ 6（handler 成功）。
        try {
            await handler(message);
        } catch (e) {
            return {
                ...base,
                kind: 'handler_error',
                matchedRule: label,
                detail: e instanceof Error ? e.message : 'Unknown error',
                skipped,
            };
        }

        if (!fallthrough) {
            return { ...base, kind: 'responded', matchedRule: label, skipped };
        }
        // fallthrough=true：handler 已执行（已响应），继续往下试更多规则。
        // 终态记最后一次成功响应，循环结束后直接用 lastResponded。
        lastResponded = { ...base, kind: 'responded', matchedRule: label, skipped };
    }

    // 退出路径 7：循环走完。要么有过 fallthrough 响应（responded），要么
    // 无任何规则匹配（no_match）。两者都是明确可查终态，绝无静默 return。
    if (lastResponded) return lastResponded;
    return { ...base, kind: 'no_match', skipped };
}

// 真实链路入口：组装依赖（multiBotManager 取 botRole、真实 NotBlocked）后
// 调 runRulesWith，并把唯一终态记录落成可查日志（决策四：禁止静默丢弃，
// 每条消息无论走哪条退出路径都有一条可查记录）。
export async function runRules(message: RuleMessage): Promise<RuleTerminalState> {
    const botRole = botDirectory.getBotConfig(context.getBotName() || '')?.bot_role;

    // 指令来源从「engine 硬编码 chatRules 常量」改成「CommandRegistry.forChannel」：
    // 该 channel 的平台指令在前（由各插件 import 期注册）+ 核心通用聊天主链路在后。
    // engine 不再认识任何具体平台指令——归属靠注册，不靠 channels flag。
    const state = await runRulesWith(message, {
        chatRules: getCommandRegistry().forChannel(message.channel),
        botRole,
        notBlocked: NotBlocked,
    });

    logTerminalState(state);
    return state;
}

function logTerminalState(s: RuleTerminalState): void {
    const head =
        `[runRules] terminal=${s.kind} channel=${s.channel} ` +
        `message=${s.messageId} chat=${s.chatId} user=${s.userId}`;
    const tail =
        (s.matchedRule ? ` matched="${s.matchedRule}"` : '') +
        (s.detail ? ` detail="${s.detail}"` : '') +
        (s.skipped.length > 0 ? ` skipped=[${s.skipped.join(' | ')}]` : '');
    if (s.kind === 'handler_error' || s.kind === 'rule_error') {
        console.error(head + tail);
    } else {
        console.info(head + tail);
    }
}
