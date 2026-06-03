// 飞书事件进入本进程入站链路的唯一收口。webhook 入口（plugins/lark/webhook）调它，
// 分发逻辑只此一份：
//   1. MongoDB 审计落库（fire-and-forget，不阻塞）
//   2. 确保 handler 已注册进 EventRegistry
//   3. 在 bot context（botName + traceId + lane）内按 event_type 找 handler 异步执行
//
// 异步执行 + 立即返回：飞书要求 webhook 快速 ACK，真正处理 fire-and-forget。

import { insertEvent } from '@dal/mongo/client';
import { context } from '@middleware/context';
import {
    EventRegistry,
    registerEventHandlerInstance,
} from '@plugins/lark/events/event-registry';
import { larkEventHandlers } from '@plugins/lark/events/handlers';

let handlersInitialized = false;
function ensureHandlersInitialized(): void {
    if (!handlersInitialized) {
        registerEventHandlerInstance(larkEventHandlers);
        handlersInitialized = true;
        console.info('[lark-dispatch] event handlers initialized');
    }
}

export interface LarkEventInput {
    eventType: string;
    params: unknown;
    botName?: string;
    traceId?: string;
    // 跨 lane 消费侧从信封读出 lane 注入；本进程 webhook 入口不带（默认本 lane）。
    lane?: string;
}

export async function dispatchLarkEvent(input: LarkEventInput): Promise<void> {
    // 1. 审计落库（fire-and-forget）
    insertEvent(input.params as Record<string, unknown>).catch((err) => {
        console.error('[lark-dispatch] insert event error:', err);
    });

    // 2. 确保 handler 已注册
    ensureHandlersInitialized();

    // 3. 在 bot context 内异步执行 handler
    const ctx = context.createContext(input.botName, input.traceId, input.lane);
    await context.run(ctx, async () => {
        const handler = EventRegistry.getHandlerByEventType(input.eventType);
        if (handler) {
            handler(input.params).catch((err) => {
                console.error(`[lark-dispatch] handler ${input.eventType} failed:`, err);
            });
        } else {
            console.warn(`[lark-dispatch] no handler for event_type: ${input.eventType}`);
        }
    });
}
