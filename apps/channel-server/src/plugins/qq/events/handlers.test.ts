// QQ 入站编排里，泳道交接必须发生在投影锁**之外**。
//
// 那把锁是 Redis 的（`lock:qq:message-projection:{qq_message_id}`，见
// common-projector.ts），prod 与泳道进程共用同一个 Redis。交接现在是 MQ 异步
// publish，所以持锁投递看不出问题；一旦交接改成"同步等接收端处理完"，接收端重走投影
// 就会去抢同一条消息的锁，而投递端要等交接返回才放锁 —— 两边互等到窗口超时，表现是
// 泳道消息全部超时失败。所以这条边界只能靠时序断言钉住。
//
// 用 mock.module 是因为编排里的锁和投递都是模块级导入。bun 的 mock.module 是整模块
// 替换 + 进程级全局（mock.restore() 撤不掉），所以两个 mock 都先抓真身、afterAll 放
// 回去，否则同一轮里后跑的文件会拿到残缺模块。

import { afterAll, beforeEach, describe, expect, it, mock } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';
import { botDirectory } from '@inner/shared/bot';
import type { CustomInboundMessage } from '@inner/shared/protocols';
import { context } from '@middleware/context';

import type { QqInboundProjection } from '../common-projector';

const BOT_NAME = 'chiwei-qq';
const QQ_MESSAGE_ID = 'msg_10001';

/** 编排里发生的事，按发生顺序。 */
const trace: string[] = [];

const projection: QqInboundProjection = {
    commonUserId: 'cu-1',
    commonConversationId: 'cc-1',
    commonMessageId: 'cm-1',
    commonRootMessageId: 'cm-1',
    commonReplyMessageId: undefined,
    senderDisplayName: undefined,
    mentionedUserIds: [],
    content: [{ kind: 'text', text: '你好' }],
    contentText: '你好',
    scope: 'direct',
};

const envelope = {
    channel: 'qq',
    event_type: 'qq.message.receive',
    global_message_id: projection.commonMessageId,
    trace_id: 'trace-1',
    lane: 'ppe-foo',
    bot_name: BOT_NAME,
    params: {},
};

let prepareImpl: () => Promise<QqInboundProjection> = async () => projection;
let storeImpl: () => Promise<void> = async () => {};

const realCommonProjector = { ...(await import('../common-projector')) };
mock.module('../common-projector', () => ({
    ...realCommonProjector,
    withQqInboundProjectionLock: async <T>(id: string, task: () => Promise<T>): Promise<T> => {
        trace.push(`acquire:${id}`);
        try {
            return await task();
        } finally {
            trace.push(`release:${id}`);
        }
    },
    prepareQqInboundProjection: (): Promise<QqInboundProjection> => prepareImpl(),
    storeQqInboundMessage: (): Promise<void> => {
        trace.push('store');
        return storeImpl();
    },
}));

/** 泳道判定拿到的输入，按调用顺序。 */
const handoffInputs: { handedOff: boolean; currentLane: string }[] = [];
/** null = 这条归本进程处理，编排继续往下走到落库那一段。 */
let handoffResult: typeof envelope | null = envelope;

const realDispatch = { ...(await import('@integrations/lane-handoff')) };
mock.module('@integrations/lane-handoff', () => ({
    ...realDispatch,
    resolveInboundLaneHandoff: async (ctx: { handedOff: boolean; currentLane: string }) => {
        handoffInputs.push({ handedOff: ctx.handedOff, currentLane: ctx.currentLane });
        return handoffResult;
    },
    handOffToLane: async (): Promise<void> => {
        trace.push('hand-off');
    },
}));

const realRules = { ...(await import('@inner/shared/rules')) };
mock.module('@inner/shared/rules', () => ({
    ...realRules,
    // 规则引擎本身有自己的用例。这里只要它别去连 DB，并且别产出
    // pendingChatTrigger —— 落库之后那一段有真 Redis / 真 MQ。
    runRules: async () => ({ kind: 'no_match', skipped: [], pendingChatTrigger: undefined }),
}));

type MutableBotDirectory = { botConfigs: Map<string, BotConfig> };
const originalBotConfigs = new Map((botDirectory as unknown as MutableBotDirectory).botConfigs);

afterAll(() => {
    mock.module('../common-projector', () => realCommonProjector);
    mock.module('@integrations/lane-handoff', () => realDispatch);
    mock.module('@inner/shared/rules', () => realRules);
    (botDirectory as unknown as MutableBotDirectory).botConfigs = new Map(originalBotConfigs);
});

const { qqEventHandlers } = await import('./handlers');

function qqBot(): BotConfig {
    return {
        bot_name: BOT_NAME,
        channel: 'qq',
        common_user_id: 'cu-bot',
        is_active: true,
    } as BotConfig;
}

function inboundMessage(): CustomInboundMessage {
    return {
        botName: BOT_NAME,
        chatType: 'direct',
        conversationId: 'user_001',
        senderId: 'user_001',
        text: '你好',
        messageId: QQ_MESSAGE_ID,
        timestamp: '2026-06-27T10:00:00+08:00',
    };
}

function resetOrchestration(): void {
    trace.length = 0;
    handoffInputs.length = 0;
    prepareImpl = async () => projection;
    storeImpl = async () => {};
    handoffResult = envelope;
    (botDirectory as unknown as MutableBotDirectory).botConfigs = new Map([[BOT_NAME, qqBot()]]);
}

describe('QQ 入站：泳道交接与投影锁的边界', () => {
    beforeEach(resetOrchestration);

    it('交接发生在投影锁释放之后', async () => {
        await context.run(context.createContext(BOT_NAME, 'trace-1'), () =>
            qqEventHandlers.handleInbound(inboundMessage()),
        );

        expect(trace).toEqual([`acquire:${QQ_MESSAGE_ID}`, `release:${QQ_MESSAGE_ID}`, 'hand-off']);
    });

    it('qq-gateway 投来的原始消息：泳道判定按「未交接」跑', async () => {
        await context.run(context.createContext(BOT_NAME, 'trace-1'), () =>
            qqEventHandlers.handleInbound(inboundMessage()),
        );

        expect(handoffInputs).toEqual([{ handedOff: false, currentLane: 'prod' }]);
    });

    it('交接过来的信封：泳道判定按「已交接」跑（阻断自投循环）', async () => {
        await context.run(context.createContext(BOT_NAME, 'trace-1'), () =>
            qqEventHandlers.handleInbound(inboundMessage(), { handedOff: true }),
        );

        expect(handoffInputs).toEqual([{ handedOff: true, currentLane: 'prod' }]);
    });

    it('处理失败往上抛，不再吞掉（调用方要据此返回非 2xx）', async () => {
        prepareImpl = async () => {
            throw new Error('projection lock timeout');
        };

        await expect(
            context.run(context.createContext(BOT_NAME, 'trace-1'), () =>
                qqEventHandlers.handleInbound(inboundMessage()),
            ),
        ).rejects.toThrow('projection lock timeout');
    });
});

// 编排里有两处"记一条 error 日志然后 return"。它们在 MQ 时代等于 ACK 丢弃；现在两个
// 调用方都是 HTTP 端点（/api/internal/qq/inbound 与 .../lane-inbound），return 等于回
// 200，投递方据此认为这条消息处理完了 —— 而它既没落库也没发 ChatTrigger。
//
// 这一组用例走的是编排里的**真实**分支（不是让替身抛错）：botDirectory 里真的没有这个
// bot、storeQqInboundMessage 真的失败。
describe('QQ 入站：真失败必须上抛，不能让接收端谎报成功', () => {
    beforeEach(resetOrchestration);

    it('bot 的 channel plugin 解析不出来时上抛', async () => {
        (botDirectory as unknown as MutableBotDirectory).botConfigs = new Map();

        await expect(
            context.run(context.createContext(BOT_NAME, 'trace-1'), () =>
                qqEventHandlers.handleInbound(inboundMessage()),
            ),
        ).rejects.toThrow(/bot config not found/);
    });

    it('bot 身份还没初始化（没有 common_user_id）时上抛', async () => {
        (botDirectory as unknown as MutableBotDirectory).botConfigs = new Map([
            [BOT_NAME, { ...qqBot(), common_user_id: undefined } as unknown as BotConfig],
        ]);

        await expect(
            context.run(context.createContext(BOT_NAME, 'trace-1'), () =>
                qqEventHandlers.handleInbound(inboundMessage()),
            ),
        ).rejects.toThrow(/common_user_id/);
    });

    it('落库失败时上抛，不再当成处理成功', async () => {
        handoffResult = null;
        storeImpl = async () => {
            throw new Error('relation "qq_message" does not exist');
        };

        await expect(
            context.run(context.createContext(BOT_NAME, 'trace-1'), () =>
                qqEventHandlers.handleInbound(inboundMessage()),
            ),
        ).rejects.toThrow(/qq_message/);
        expect(trace).toContain('store');
    });

    // 对照组：这条**不是**失败。报文里没有任何可处理内容（adapter 解析出 null）说明
    // 它压根不是一条消息，投递方重发也还是同样的结果，回 2xx 是对的。
    it('报文不是消息（adapter 解析出 null）时正常返回，不上抛', async () => {
        await context.run(context.createContext(BOT_NAME, 'trace-1'), () =>
            qqEventHandlers.handleInbound({ ...inboundMessage(), text: '' }),
        );

        expect(trace).toEqual([]);
        expect(handoffInputs).toEqual([]);
    });
});
