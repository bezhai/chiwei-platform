// 卡片按钮被按下之后。**这是入站的第二条路。**
//
//     飞书 ──/webhook/{bot}/card 或长连 ──▶ card.action.trigger ──▶ 本文件
//
// 它跟指令那条路完全不搭界：不过规则引擎、不看 @、不看是不是纯文本，只认按钮里带回
// 来的那份 value（见 card-actions.ts）。所以它不在指令清单上，而是直接挂在入站的事件
// 处理表里 —— 拆分之后这条路曾经整条断着，路由和事件槽都在，处理表里却没有它。
//
// ## 三种动作，两个开放平台端点
//
//   换一批 / 今日新图的换一批   /open-apis/interactive/v1/card/update   延时更新，只给按的人
//   查看详情（群里）            /open-apis/ephemeral/v1/send            仅操作者可见
//   查看详情（私聊）            普通的卡片回复
//
// 前两个飞书 SDK 里没有对应方法，所以走端口的逃生口（见 outbound/lark-api.ts 的
// `request`）。
//
// ## 每条分支都自己吞错
//
// 回调这条路上**没有能对着说话的人**：用户按了按钮，我们要么更新那张卡片、要么什么都
// 不做。抛出去只会变成入口的一条 error 日志，还会让 ack 之后的异步链路留下 unhandled
// rejection。与拆分前一致。

import type { LarkOutboundApi } from '../outbound/lark-api';
import type { LarkChatRow } from '../projection/tables';
import {
    FETCH_PHOTO_DETAILS,
    UPDATE_DAILY_PHOTO_CARD,
    UPDATE_PHOTO_CARD,
    type LarkCardAction,
} from './card-actions';
import { newPhotoCard, photoDetailCard, photoSearchCard } from './cards';
import type { LarkReadyPhotos } from './ready';

/** 延时更新一张卡片。SDK 没封这个接口。 */
const CARD_UPDATE = '/open-apis/interactive/v1/card/update';
/** 只有指定的人看得见的一条消息。SDK 也没封。 */
const EPHEMERAL_SEND = '/open-apis/ephemeral/v1/send';

export interface LarkCardCallbackDeps {
    api: Pick<LarkOutboundApi, 'replyCard' | 'request'>;
    /** 只要一件事：这个会话是私聊还是群，以及它开了哪些开关。 */
    store: { larkChat(chatId: string): Promise<LarkChatRow | null> };
    photos: LarkReadyPhotos;
}

/** 报文长得像一次卡片交互吗。别的卡片（别人发的、老版本的）也会打到同一个回调上。 */
function asCardAction(payload: unknown): LarkCardAction | null {
    const action = payload as LarkCardAction | null;
    if (!action || typeof action !== 'object') return null;
    if (!action.action || !action.context || !action.operator) return null;
    return action;
}

export async function handleLarkCardAction(
    deps: LarkCardCallbackDeps,
    payload: unknown,
): Promise<void> {
    const action = asCardAction(payload);
    if (!action) {
        console.warn('[lark-photo] card callback payload is not a card action:', payload);
        return;
    }

    const value = action.action.value;
    switch (value?.type) {
        case UPDATE_PHOTO_CARD:
            await refresh(deps, action, (allowLimitPhoto) =>
                photoSearchCard(deps.photos, value.tags, allowLimitPhoto),
            );
            return;
        case UPDATE_DAILY_PHOTO_CARD:
            await refresh(deps, action, (allowLimitPhoto) =>
                newPhotoCard(deps.photos, value.start_time, allowLimitPhoto),
            );
            return;
        case FETCH_PHOTO_DETAILS:
            await detail(deps, action, value.images);
            return;
        default:
            console.warn('[lark-photo] nobody handles this card action:', value);
    }
}

/** 卡片元素的形状由卡片库定，这一层只负责把它交给飞书。 */
type CardElements = { getElements(): unknown[] };

/**
 * 重建这张卡片，延时更新给按按钮的那个人。
 *
 * **只更给他一个人**（`open_ids`）：少了这一项，整个群都会看到别人换出来的那一批。
 */
async function refresh(
    deps: LarkCardCallbackDeps,
    action: LarkCardAction,
    build: (allowLimitPhoto: boolean | undefined) => Promise<CardElements>,
): Promise<void> {
    try {
        const chat = await deps.store.larkChat(action.context.open_chat_id);
        const card = await build(chat?.permission_config?.allow_send_limit_photo);

        await deps.api.request('POST', CARD_UPDATE, {
            // 一次性凭证，飞书用它认出要更新的是哪张卡片。
            token: action.token,
            card: { open_ids: [action.operator.open_id], elements: card.getElements() },
        });
    } catch (error) {
        console.error('[lark-photo] could not refresh the card:', error);
    }
}

/**
 * 把这批图的作者与标签摊开。
 *
 * 群里走"仅操作者可见"，**私聊和查不到会话行时**才直接回复 —— 群里直接回复的话，
 * 一个人好奇就会把详情刷给所有人。查不到会话行按私聊处理是拆分前的形态，照搬。
 */
async function detail(
    deps: LarkCardCallbackDeps,
    action: LarkCardAction,
    pixivAddrs: string[],
): Promise<void> {
    try {
        // 两件事同时起步：查会话不依赖卡片、拼卡片不依赖会话。
        const [chat, card] = await Promise.all([
            deps.store.larkChat(action.context.open_chat_id),
            photoDetailCard(deps.photos, pixivAddrs),
        ]);

        if (!chat || chat.chat_mode === 'p2p') {
            await deps.api.replyCard(action.context.open_message_id, card, false);
            return;
        }

        await deps.api.request('POST', EPHEMERAL_SEND, {
            chat_id: action.context.open_chat_id,
            msg_type: 'interactive',
            card,
            open_id: action.operator.open_id,
        });
    } catch (error) {
        console.error('[lark-photo] could not show the photo details:', error);
    }
}
