import { Message } from 'core/models/message';
import { replyTemplate } from '@lark/basic/message';
import { CommandHandler, CommandRule } from './admin/command-handler';
import { deleteBotMessage } from './admin/delete-message';
import { genHistoryCard } from './general/gen-history';
import { checkMeme, genMeme } from '@core/services/media/meme/meme';
import { changeRepeatStatus, repeatMessage } from './group/repeat-message';
import {
    EqualText,
    NeedNotRobotMention,
    NeedRobotMention,
    OnlyGroup,
    RegexpMatch,
    RuleConfig,
    TextMessageLimit,
    WhiteGroupCheck,
    IsAdmin,
    NotBlocked,
} from './rule';
import { sendPhoto } from '@core/services/media/photo/send-photo';
import { makeTextReply } from 'core/services/ai/reply';
import { sendBalance } from './admin/balance';
import { context } from '@middleware/context';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';

// 工具函数：执行规则链
export async function runRules(message: Message) {
    // 黑名单检查：被拉黑的用户直接忽略
    if (!(await NotBlocked(message))) {
        console.info(`Blocked user ${message.sender} message ignored`);
        return;
    }

    // 多 bot 分工：灰度开启时，按 bot 角色过滤规则
    const multiBotEnabled = message.basicChatInfo?.gray_config?.multi_bot === 'enabled';
    const botRole = multiBotEnabled
        ? multiBotManager.getBotConfig(context.getBotName() || '')?.bot_role
        : undefined;

    for (const { rules, handler, fallthrough, async_rules, category } of chatRules) {
        // 灰度开启且规则有分类时，只执行匹配角色的规则
        if (botRole && category && category !== botRole) {
            continue;
        }
        // 检查同步规则
        const syncRulesPass = rules.every((rule) => rule(message));

        // 检查异步规则
        const asyncRulesPass = async_rules
            ? (await Promise.all(async_rules.map((rule) => rule(message)))).every(
                  (result) => result,
              )
            : true;

        // 如果所有规则（同步和异步）都通过
        if (syncRulesPass && asyncRulesPass) {
            try {
                await handler(message);
            } catch (e) {
                console.error('rule engine error:', {
                    message: e instanceof Error ? e.message : 'Unknown error',
                    stack: e instanceof Error ? e.stack : undefined,
                });
            }

            if (!fallthrough) break;
        }
    }
}

// 定义规则和对应处理逻辑
const chatRules: RuleConfig[] = [
    {
        rules: [
            NeedNotRobotMention,
            OnlyGroup,
            WhiteGroupCheck((chatInfo) => chatInfo.permission_config?.open_repeat_message ?? false),
        ],
        handler: repeatMessage,
        fallthrough: true,
        comment: '复读功能',
        category: 'utility',
    },
    {
        rules: [EqualText('余额'), TextMessageLimit, NeedRobotMention, IsAdmin],
        handler: sendBalance,
        comment: '发送余额信息',
        category: 'utility',
    },
    {
        rules: [EqualText('帮助'), TextMessageLimit, NeedRobotMention],
        handler: async (message) => {
            replyTemplate(message.messageId, 'ctp_AAYrltZoypBP', undefined);
        },
        comment: '给用户发送帮助信息',
        category: 'utility',
    },
    {
        rules: [EqualText('撤回'), TextMessageLimit, NeedRobotMention],
        handler: deleteBotMessage,
        comment: '撤回消息',
        category: 'utility',
    },
    {
        rules: [EqualText('水群', '水群趋势'), TextMessageLimit, NeedRobotMention],
        handler: genHistoryCard,
        comment: '生成水群历史卡片',
        category: 'utility',
    },
    {
        rules: [EqualText('开启复读'), TextMessageLimit, NeedRobotMention, OnlyGroup],
        handler: changeRepeatStatus(true),
        category: 'utility',
    },
    {
        rules: [EqualText('关闭复读'), TextMessageLimit, NeedRobotMention, OnlyGroup],
        handler: changeRepeatStatus(false),
        category: 'utility',
    },
    {
        rules: [CommandRule, TextMessageLimit, NeedRobotMention],
        handler: CommandHandler,
        comment: '指令处理',
        category: 'utility',
    },
    {
        rules: [RegexpMatch('^发图'), TextMessageLimit, NeedRobotMention],
        handler: sendPhoto,
        comment: '发送图片',
        category: 'utility',
    },
    {
        rules: [NeedRobotMention],
        async_rules: [checkMeme],
        handler: genMeme,
        comment: 'Meme',
        category: 'utility',
    },
    {
        rules: [NeedRobotMention],
        handler: makeTextReply,
        comment: '聊天',
        category: 'persona',
    },
];
