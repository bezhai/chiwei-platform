// 飞书平台专属指令清单（10 条）。从 engine.ts 的硬编码 chatRules 搬来——
// 内部业务逻辑一行未改，只是：(1) 物理搬进 plugins/lark；(2) 去掉 channels
// flag（归属靠注册，不靠 flag）。这些 handler 本就 import 飞书 SDK，落在
// plugins/ 下是合法的（plugins 允许独占平台 SDK）。
//
// 顺序契约：utility 指令在前、聊天主链路（核心通用、不在这里）在后由
// CommandRegistry.forChannel 拼接。聊天主链路是 NeedRobotMention 的 catch-all，
// 必须让这些 utility 指令先获得匹配机会。

import { replyTemplate } from '@lark/basic/message';
import { CommandHandler, CommandRule } from './commands/command-handler';
import { deleteBotMessage } from './commands/delete-message';
import { genHistoryCard } from './commands/gen-history';
import { changeRepeatStatus, repeatMessage } from './commands/repeat-message';
import { sendBalance } from './commands/balance';
import { checkMeme, genMeme } from './services/meme/meme';
import { sendPhoto } from './services/photo/send-photo';
import {
    EqualText,
    NeedNotRobotMention,
    NeedRobotMention,
    OnlyGroup,
    RegexpMatch,
    RuleConfig,
    TextMessageLimit,
} from '@core/rules/rule';
import { WhiteGroupCheck, IsAdmin } from './lark-rules';
import { larkContextStore } from './lark-context-store';

export const larkCommands: RuleConfig[] = [
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
            const lark = larkContextStore.get(message.commonMessageId);
            replyTemplate(lark.messageId, 'ctp_AAYrltZoypBP', undefined);
        },
        comment: '给用户发送帮助信息',
        category: 'utility',
    },
    {
        rules: [EqualText('撤回'), TextMessageLimit, NeedRobotMention],
        handler: deleteBotMessage,
        comment: '撤回消息',
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
        comment: '开启复读',
    },
    {
        rules: [EqualText('关闭复读'), TextMessageLimit, NeedRobotMention, OnlyGroup],
        handler: changeRepeatStatus(false),
        category: 'utility',
        comment: '关闭复读',
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
];
