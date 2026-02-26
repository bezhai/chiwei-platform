import { LarkBaseChatInfo } from 'infrastructure/dal/entities';
import { Message } from 'core/models/message';
import { getBotUnionId } from '@core/services/bot/bot-var';
import { UserBlacklistRepository } from '@infrastructure/dal/repositories/repositories';

// 定义规则函数类型
type Rule = (message: Message) => boolean;

type AsyncRule = (message: Message) => Promise<boolean>;

// 定义权限函数类型
type Handler = (message: Message) => Promise<void>;

// 组合规则返回的类型
export interface GenCombineRule {
    rule: Rule; // 一个函数，判断消息是否满足至少一个规则
    handler: Handler; // 一个异步函数，依次执行满足规则的处理函数
}

/**
 * combineRule
 *
 * 该函数用于将一组规则与处理器组合成一个通用的规则和处理器。
 * 它可以根据提供的规则和适配器函数，生成一个组合规则对象。
 *
 * @template T - 规则键的类型，可以是任意类型（如字符串、数字或对象）。
 *
 * @param originRule - 一个规则数组，每个规则包含：
 *   - `key`: 规则的键，类型为泛型 `T`。
 *   - `handler`: 一个异步处理函数，当规则被触发时会执行。
 *
 * @param adaptor - 一个适配器函数，它将规则的 `key` 转换为一个 `Rule` 函数。
 *   - 该 `Rule` 函数接收一个 `message` 对象，并返回一个布尔值，表示消息是否满足规则。
 *
 * @returns {GenCombineRule} - 返回一个组合规则对象，包含：
 *   - `rule`: 一个函数，用于判断消息是否满足任意一个规则。
 *   - `handler`: 一个异步函数，用于执行所有满足规则的处理器。
 *
 * @example
 * const originRules = [
 *   { key: "rule1", handler: async (message) => console.info("Handled rule1") },
 *   { key: "rule2", handler: async (message) => console.info("Handled rule2") },
 * ];
 *
 * const adaptor = (key: string) => (message: Message) => message.text().includes(key);
 *
 * const combined = combineRule(originRules, adaptor);
 *
 * const message = { content: "This is a test for rule1" };
 *
 * if (combined.rule(message)) {
 *   combined.handler(message); // Output: "Handled rule1"
 * }
 */
export const combineRule = <T>(
    originRule: { key: T; handler: Handler }[],
    adaptor: (key: T) => Rule,
): GenCombineRule => {
    const rule: Rule = (message) => originRule.some((rule) => adaptor(rule.key)(message));
    const handler: Handler = async (message) => {
        for (const rule of originRule) {
            if (adaptor(rule.key)(message)) {
                await rule.handler(message);
            }
        }
    };
    return { rule, handler };
};

// 定义规则和对应处理逻辑的结构
export interface RuleConfig {
    rules: Rule[];
    async_rules?: AsyncRule[];
    handler: Handler;
    fallthrough?: boolean;
    comment?: string;
}

// 工具函数：通用规则
export const NeedRobotMention: Rule = (message) =>
    message.hasMention(getBotUnionId()) || message.isP2P();

export const NeedNotRobotMention: Rule = (message) => !NeedRobotMention(message);

export const TextMessageLimit: Rule = (message) => message.isTextOnly();

export const ContainKeyword =
    (keyword: string): Rule =>
    (message) =>
        message.text().includes(keyword);

export const EqualText =
    (...texts: string[]): Rule =>
    (message) =>
        texts.some((text) => message.clearText() === text);

export const RegexpMatch =
    (pattern: string): Rule =>
    (message) => {
        try {
            return new RegExp(pattern).test(message.clearText());
        } catch {
            return false;
        }
    };

export const OnlyP2P: Rule = (message) => message.isP2P();

export const OnlyGroup: Rule = (message) => !message.isP2P();

export const WhiteGroupCheck =
    (checkFunc: (chatInfo: LarkBaseChatInfo) => boolean): Rule =>
    (message) => {
        const chatInfo = message.basicChatInfo;
        return chatInfo ? checkFunc(chatInfo) : false;
    };

export const IsAdmin: Rule = (message) => message.senderInfo?.is_admin ?? false;

// 异步规则：检查用户是否未被拉黑
export const NotBlocked: AsyncRule = async (message) => {
    const unionId = message.sender;
    if (!unionId || unionId === 'unknown_sender') return true;

    const blocked = await UserBlacklistRepository.findOne({
        where: { union_id: unionId },
    });
    return !blocked;
};
