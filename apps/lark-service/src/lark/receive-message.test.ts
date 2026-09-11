// 一条飞书消息进来之后，本服务按什么顺序做事。
//
// 这里钉的就是「落账成功了才跑规则」这一条 —— 它是与拆分前的一处**有意**的差别，
// 见 receive-message.ts 顶部。

import { describe, expect, it } from 'bun:test';

import type { LarkEvent } from './ingress/lark-event';
import type { LarkBotLookup } from './message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from './message/read-message-event';
import type { LarkMessageEvent } from './message/wire';
import type {
    LarkInboundProjection,
    LarkRecordedInbound,
} from './projection/inbound-projection';
import { receiveLarkMessage, type LarkReceiveDeps } from './receive-message';

const bots: LarkBotLookup = { byAppId: () => null, byUnionId: () => null };

function reading(): LarkMessageReading {
    const event: LarkMessageEvent = {
        app_id: 'cli_chiwei',
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: '{"text":"hi"}',
        },
    };
    return readLarkMessageEvent(event, bots)!;
}

const event: LarkEvent = {
    type: 'im.message.receive_v1',
    payload: {},
    botName: 'chiwei',
    receivedAt: new Date('2026-09-04T06:50:54.000Z'),
    traceId: 'trace-1',
};

const projection: LarkInboundProjection = {
    commonUserId: 'cu_sender',
    commonConversationId: 'cc_1',
    commonMessageId: 'cm_1',
    commonRootMessageId: 'cm_1',
    commonReplyMessageId: undefined,
    mentionedCommonUserIds: [],
};

const recorded: LarkRecordedInbound = {
    projection,
    commands: {
        appId: 'cli_chiwei',
        isAdmin: true,
        permission: { open_repeat_message: true },
        groupChat: null,
    },
};

function wire(overrides: Partial<LarkReceiveDeps> = {}) {
    const trace: string[] = [];
    const sawRecorded: LarkRecordedInbound[] = [];
    const cached: LarkRecordedInbound[] = [];
    const deps: LarkReceiveDeps = {
        project: async () => {
            trace.push('project');
            return { kind: 'recorded', ...recorded };
        },
        cacheAttachments: (_reading, seen) => {
            trace.push('attachments');
            cached.push(seen);
        },
        applyRules: async (_reading, seen) => {
            trace.push('rules');
            sawRecorded.push(seen);
        },
        ...overrides,
    };
    return { deps, trace, sawRecorded, cached };
}

describe('receiveLarkMessage', () => {
    it('先落账，再缓存附件，再跑规则', async () => {
        const wired = wire();

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.trace).toEqual(['project', 'attachments', 'rules']);
    });

    // 附件缓存整条被摘掉不会有任何症状：入站照常、赤尾照常回话，只是对象存储里再也
    // 不落新附件，几天后才会有人发现读小说读不到东西。所以它在流程里的存在本身要有
    // 断言钉住（上一条的 trace 顺序 + 这一条的入参）。
    it('附件缓存拿到的是投影产出的那份事实（gate 要用群资料）', async () => {
        const wired = wire();

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.cached.map((seen) => seen.projection)).toEqual([projection]);
        expect(wired.cached.map((seen) => seen.commands)).toEqual([recorded.commands]);
    });

    // 旁路的硬约束：它绝不能让一条消息处理不下去。缓存这一步炸了，规则照跑、入站照常
    // 返回 —— 反过来的话，tool-service 一挂，赤尾就在所有带图的消息上集体失声。
    it('附件缓存抛错不影响规则，也不让入站失败', async () => {
        const wired = wire({
            cacheAttachments: () => {
                wired.trace.push('attachments');
                throw new Error('tool-service client is on fire');
            },
        });

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.trace).toEqual(['project', 'attachments', 'rules']);
    });

    it('规则拿到的是投影产出的那组公共层 id', async () => {
        const wired = wire();

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.sawRecorded.map((seen) => seen.projection)).toEqual([projection]);
    });

    // 投影顺路读到的指令事实同样要往下带。在这里被截掉的症状是静默的：指令照样跑，
    // 只是每条都得自己再查一遍库，而"搭车读省一次查询"那个设计就白做了。
    it('规则也拿到投影顺路读到的指令事实', async () => {
        const wired = wire();

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.sawRecorded.map((seen) => seen.commands)).toEqual([recorded.commands]);
    });

    // 与拆分前的差别（有意）：拆分前规则跑在落库之前，落库失败时用户已经看到了指令
    // 的回复、库里却没有这条消息。这里落账失败则规则根本不跑，错误照常往上抛 ——
    // 泳道交接据此应答非 2xx，飞书那两个入口留下一条可查错误。
    it('落账失败时规则根本不跑，错误往上抛', async () => {
        const wired = wire({
            project: async () => {
                throw new Error('common_message is on fire');
            },
        });

        await expect(receiveLarkMessage(wired.deps, reading(), event)).rejects.toThrow(
            'common_message is on fire',
        );
        expect(wired.trace).toEqual([]);
    });

    // 交给别的泳道的消息本进程不再处理：规则、指令、**附件缓存**全归目标
    // 泳道。附件在这里跑掉的话，prod 会替泳道把附件下载一遍写进 prod 的对象存储，而目标
    // 泳道消费信封之后还会再下载一遍。
    it('这条该走别的泳道时到此为止，附件也不缓存', async () => {
        const wired = wire({
            project: async () => {
                wired.trace.push('project');
                return { kind: 'handed-off', lane: 'ppe-x' };
            },
        });

        await receiveLarkMessage(wired.deps, reading(), event);

        expect(wired.trace).toEqual(['project']);
    });
});
