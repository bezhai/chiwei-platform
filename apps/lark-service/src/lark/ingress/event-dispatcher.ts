// 飞书 SDK 的事件分发器。webhook 和长连是两条传输，但**验签、解密、报文展开**
// 是同一套，都由 SDK 这个对象做，所以两边共用这里的构造。
//
// 我们向飞书订阅了哪些事件，就是这一份清单。**同一个回调注册给所有类型** —— 谁
// 认领哪个类型由装配时那张处理表说了算（见 lark-event.ts），在这里再分一次会变成
// 两处清单，改一处忘一处。

import { EventDispatcher } from '@larksuiteoapi/node-sdk';

import type { LarkCredentials } from '../credentials';
import type { LarkEventSink } from './event-sink';

const SUBSCRIBED_EVENTS = [
    'im.message.receive_v1',
    'im.message.recalled_v1',
    'im.chat.member.user.added_v1',
    'im.chat.member.user.deleted_v1',
    'im.chat.member.user.withdrawn_v1',
    'im.chat.member.bot.added_v1',
    'im.chat.member.bot.deleted_v1',
    'im.message.reaction.created_v1',
    'im.message.reaction.deleted_v1',
    'im.chat.access_event.bot_p2p_chat_entered_v1',
    'im.chat.updated_v1',
    'card.action.trigger',
] as const;

export function createEventDispatcher(
    credentials: LarkCredentials,
    sink: LarkEventSink,
): EventDispatcher {
    const handlers: Record<string, (payload: unknown) => Record<string, never>> = {};
    for (const eventType of SUBSCRIBED_EVENTS) {
        handlers[eventType] = (payload) => sink.onEvent(payload);
    }
    return new EventDispatcher({
        verificationToken: credentials.verification_token,
        encryptKey: credentials.encrypt_key,
    }).register(handlers);
}
