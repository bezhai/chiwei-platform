import { context } from '@inner/shared/middleware';
import { chooseInboundLane } from './ingress/lane-handoff';
import { UnprocessableLarkEvent, type LarkEvent } from './ingress/lark-event';
import type { InboundLaneEnvelope } from './ingress/lane-envelope';

export interface LarkMemberChangeDeps {
    currentLane: string;
    laneDispatchEnabled(): Promise<boolean>;
    conversationOf(chatId: string): Promise<string | undefined>;
    laneOf(botName: string, conversationId: string | undefined): Promise<string>;
    handOff(envelope: InboundLaneEnvelope): Promise<void>;
    sync(chatId: string, humanUnionIds: readonly string[]): Promise<void>;
    newId(): string;
}

/** 成员事件只携带同步触发信息；在群状态始终重新查询飞书。 */
export async function receiveLarkMemberChange(
    deps: LarkMemberChangeDeps,
    event: LarkEvent,
): Promise<void> {
    const payload = event.payload as {
        chat_id?: string;
        event_id?: string;
        users?: Array<{ user_id?: { union_id?: string } }>;
    } | null;
    if (!payload || typeof payload.chat_id !== 'string' || !payload.chat_id) {
        throw new UnprocessableLarkEvent('lark member event requires chat_id');
    }
    const chatId = payload.chat_id;
    const choice = await chooseInboundLane({
        handedOff: event.handedOff === true,
        dispatchEnabled: await deps.laneDispatchEnabled(),
        currentLane: deps.currentLane,
        laneOf: async () => deps.laneOf(event.botName, await deps.conversationOf(chatId)),
    });
    if (choice.handOff) {
        await deps.handOff({
            channel: 'lark',
            event_type: event.type,
            global_message_id: payload.event_id || deps.newId(),
            trace_id: context.getTraceId(),
            lane: choice.lane,
            bot_name: event.botName,
            params: event.payload,
            handed_off: true,
        });
        return;
    }
    const humanIds = event.type.startsWith('im.chat.member.user.')
        ? (payload.users ?? []).flatMap((user) =>
              typeof user.user_id?.union_id === 'string' && user.user_id.union_id
                  ? [user.user_id.union_id]
                  : [],
          )
        : [];
    try {
        await deps.sync(chatId, humanIds);
    } catch (error) {
        console.error(
            `[lark-directory] member sync failed bot=${event.botName} chat=${chatId} event=${event.type}:`,
            error,
        );
        throw error;
    }
}
