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
    changeMembers(
        chatId: string,
        members: readonly { unionId: string; name: string }[],
        hasLeft: boolean,
        occurredAt: Date,
    ): Promise<void>;
    newId(): string;
}

/** 使用事件中的姓名、身份和发生时间增量维护，不能依赖截断的群成员列表。 */
export async function receiveLarkMemberChange(
    deps: LarkMemberChangeDeps,
    event: LarkEvent,
): Promise<void> {
    const payload = event.payload as {
        chat_id?: string;
        event_id?: string;
        create_time?: string;
        users?: Array<{ name?: string; user_id?: { union_id?: string } }>;
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
    const timestamp = Number(payload.create_time);
    if (
        typeof payload.create_time !== 'string' ||
        !/^\d+$/.test(payload.create_time) ||
        !Number.isSafeInteger(timestamp) ||
        timestamp <= 0 ||
        !Number.isFinite(new Date(timestamp).getTime()) ||
        !Array.isArray(payload.users) ||
        payload.users.length === 0 ||
        payload.users.some(user =>
            !user || typeof user.name !== 'string' || !user.name.trim() ||
            typeof user.user_id?.union_id !== 'string' || !user.user_id.union_id.trim(),
        )
    ) {
        throw new UnprocessableLarkEvent('lark member event requires timestamp and named union IDs');
    }
    const members = payload.users.map(user => ({
        unionId: user.user_id!.union_id!,
        name: user.name!,
    }));
    const hasLeft = event.type === 'im.chat.member.user.deleted_v1' ||
        event.type === 'im.chat.member.user.withdrawn_v1';
    if (!hasLeft && event.type !== 'im.chat.member.user.added_v1') {
        throw new UnprocessableLarkEvent('unsupported lark member event');
    }
    try {
        await deps.changeMembers(chatId, members, hasLeft, new Date(timestamp));
    } catch (error) {
        console.error(
            `[lark-directory] member change failed bot=${event.botName} chat=${chatId} event=${event.type}:`,
            error,
        );
        throw error;
    }
}
