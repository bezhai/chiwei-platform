// 「发图 <标签>」。
//
//     @bot 发图 刻晴 原神
//        └──▶ 切标签 ──▶ 这个会话准不准发 ──▶ 搜图拼卡片 ──▶ 挂在原消息上回复
//
// ## 准入是三选一，不是一条开关
//
//   私聊                     一直可以（就他一个人看得到）
//   人数 ≤ 20 的群           可以（小群自己人）
//   开了 allow_send_pixiv_image  可以（管理员点头的大群）
//
// 前两条不满足又没开白名单就拒绝。判据全部来自逐消息的指令上下文（投影顺路读到的那
// 两行），这一层不再查库 —— 再查一次等于把"搭车读省一次查询"原样抵消掉。
//
// **群资料查不到（还没同步过 lark_group_chat_info）时人数未知**，落到白名单那一条。
// 与拆分前一致：那边同样是"chat_mode 是群才去查群资料"，查不到就只剩开关。
//
// ## 出错就对着用户说一句，绝不静默
//
// 三种失败（没带标签、这个群不准发、图库一张都没搜到）都变成一句话回给用户。文案是
// 线上历史，逐字照搬。**那一句进话题（inThread=true），而卡片不进** —— 拆分前就这样，
// 照搬。

import { NeedRobotMention, RegexpMatch, TextMessageLimit } from '@inner/shared/rules';
import type { RuleConfig } from '@inner/shared/rules';

import type { LarkCommandContext } from '../rules/command-context';
import type { LarkCommand, LarkCommandDeps } from '../rules/commands';
import { photoSearchCard } from './cards';

/** 大群的界线。拆分前就是 20，照搬。 */
const SMALL_GROUP = 20;

const NO_TAGS = '呜呜~要发图的话，记得带上标签告诉人家想看什么嘛(｡•́︿•̀｡)';
const TOO_MANY_PEOPLE =
    '诶嘿~这个群人有点多呢，发图功能暂时关闭啦(｡•́︿•̀｡) 想用的话可以联系开发者主人帮忙开白哦！';
const SOMETHING_BROKE = '呜呜...好像遇到奇怪的小问题了呢 (´;ω;｀) 要不稍后再试试？';

/**
 * 这个会话准不准发图。
 *
 * ## 话题群走的是群那条路，不是白名单那条
 *
 * 上游拿 `lark_base_chat_info.chat_mode === 'group'` 决定要不要查群资料，读起来像是
 * 把话题群排除在外了。**它没有**：那一列由投影 upsert（冲突键 chat_id），每收到一条
 * 消息就重写成 `'group'`。线上 905 行里只剩 2 行 `'topic'`，都是没有消息流过的死群 ——
 * 任何真的有人敲得出「发图」的话题群，上游读到的都是 `'group'`。
 *
 * 所以这里**不按 chat_type 收窄**，只问"有没有群资料这一行"。群资料对所有非私聊都会
 * 读到（投影按 `chatType !== 'p2p'` 取），与上游落到的分支一致。
 *
 * 私聊那一条用事件的 `chat_type` 而不是存储列：两者对私聊永远一致（存储列就是
 * `scope === 'direct'` 派生的），而事件字段不依赖那一行建没建。
 */
function mayPost(context: LarkCommandContext): boolean {
    if (context.message.chatType === 'p2p') return true;

    const groupChat = context.groupChat;
    if (groupChat && groupChat.user_count <= SMALL_GROUP) return true;

    return context.permission.allow_send_pixiv_image === true;
}

export function sendPhotoCommand(deps: LarkCommandDeps): LarkCommand {
    return (context) => ({
        rules: [RegexpMatch('^发图'), TextMessageLimit, NeedRobotMention],
        comment: '发送图片',
        category: 'utility',
        handler: async (message) => {
            try {
                const tags = message
                    .clearText()
                    .replace(/^发图/, '')
                    .trim()
                    .split(/\s+/)
                    .filter((tag) => tag.length > 0);
                if (tags.length <= 0) throw new Error(NO_TAGS);
                if (!mayPost(context)) throw new Error(TOO_MANY_PEOPLE);

                const card = await photoSearchCard(
                    deps.photos,
                    tags,
                    context.permission.allow_send_limit_photo,
                );
                await deps.api.replyCard(context.message.messageId, card, false);
            } catch (error) {
                await apologise(deps, context, error);
            }
        },
    });
}

/**
 * 把失败翻成一句人话。**自己再失败也不外溢**：飞书连这一句都收不下的时候，往上抛只会
 * 让引擎记一条 handler_error，用户那边已经无从补救了。
 */
async function apologise(
    deps: LarkCommandDeps,
    context: LarkCommandContext,
    error: unknown,
): Promise<void> {
    const said = error instanceof Error ? error.message : SOMETHING_BROKE;
    console.error('[lark-photo] 发图 failed:', error);
    try {
        await deps.api.replyText(context.message.messageId, said, true);
    } catch (replyError) {
        console.error('[lark-photo] could not even tell the user 发图 failed:', replyError);
    }
}
