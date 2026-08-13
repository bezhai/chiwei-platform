// 入站附件缓存。钉的是两件在线上完全静默的事：**gate 的两个分支**，和**失败不外溢**。
//
// 为什么这两条值得逐条钉：这条链路没有任何成功信号 —— 缓存不进对象存储的时候入站
// 照常工作、赤尾照常回话，只有几天后有人问「赤尾怎么读不到我发的书」才会有人来查。

import { describe, expect, it } from 'bun:test';

import {
    assembleLarkAttachments,
    cacheLarkAttachments,
    LARK_ATTACHMENT_TRACKS,
    type LarkAttachmentDeps,
} from './attachments';
import type { LarkEvent } from './ingress/lark-event';
import type { LarkContentPart } from './message/lark-content';
import type { LarkBotLookup } from './message/mentions';
import { readLarkMessageEvent, type LarkMessageReading } from './message/read-message-event';
import type { LarkMessageEvent } from './message/wire';
import type { LarkRecordedInbound } from './projection/inbound-projection';
import type { LarkGroupChatFacts } from './projection/tables';

const bots: LarkBotLookup = { byAppId: () => null, byUnionId: () => null };

const event: LarkEvent = {
    type: 'im.message.receive_v1',
    payload: {},
    botName: 'chiwei',
    traceId: 'trace-1',
};

/** 一条真的走过解析层的消息 —— 附件的 key 来自正文片段，不是测试自己编的。 */
function reading(messageType: string, content: unknown): LarkMessageReading {
    const raw: LarkMessageEvent = {
        app_id: 'cli_chiwei',
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: messageType,
            content: JSON.stringify(content),
        },
    };
    return readLarkMessageEvent(raw, bots)!;
}

/** 同一条消息，正文换成任意片段组合（一条飞书消息装不下图片 + 文件，测试里能）。 */
function carrying(...parts: LarkContentPart[]): LarkMessageReading {
    return { ...reading('text', { text: 'hi' }), content: parts };
}

const twoImages = () =>
    reading('post', {
        content: [
            [
                { tag: 'img', image_key: 'img_1' },
                { tag: 'img', image_key: 'img_2' },
            ],
        ],
    });

const oneFile = () => reading('file', { file_key: 'file_1', file_name: '书.epub' });

function recorded(groupChat: LarkGroupChatFacts | null): LarkRecordedInbound {
    return {
        projection: {
            commonUserId: 'cu_sender',
            commonConversationId: 'cc_1',
            commonMessageId: 'cm_1',
            commonRootMessageId: 'cm_1',
            mentionedCommonUserIds: [],
        },
        commands: { appId: 'cli_chiwei', isAdmin: false, permission: {}, groupChat },
    };
}

const openGroup: LarkGroupChatFacts = {
    name: '开放群',
    user_count: 5,
    download_has_permission_setting: 'all_members',
};
const closedGroup: LarkGroupChatFacts = {
    name: '锁着的群',
    user_count: 5,
    download_has_permission_setting: 'not_anyone',
};

interface Sent {
    path: string;
    body: { message_id: string; file_key: string };
    headers: Record<string, string>;
}

function recorder(overrides: Partial<LarkAttachmentDeps> = {}) {
    const sent: Sent[] = [];
    const deps: LarkAttachmentDeps = {
        post: async (path, body, headers) => {
            sent.push({ path, body, headers });
        },
        innerSecret: 'inner-secret',
        lane: undefined,
        ...overrides,
    };
    return { deps, sent };
}

describe('LARK_ATTACHMENT_TRACKS', () => {
    // 端点字面量是跨服务契约：tool-service 那侧的路由不认识别的名字，写错了发出去
    // 就是 404，而 404 被逐条吞掉、入站毫无异样。
    it('打的是 tool-service 的两个管线端点', () => {
        expect(LARK_ATTACHMENT_TRACKS.map((track) => track.path)).toEqual([
            '/api/image-pipeline/process',
            '/api/file-pipeline/process',
        ]);
    });
});

describe('cacheLarkAttachments', () => {
    it('每张图片一条 POST，打到识图管线', async () => {
        const { deps, sent } = recorder();

        await cacheLarkAttachments(deps, twoImages(), recorded(openGroup), event);

        expect(sent.map((one) => one.path)).toEqual([
            '/api/image-pipeline/process',
            '/api/image-pipeline/process',
        ]);
        expect(sent.map((one) => one.body)).toEqual([
            { message_id: 'om_1', file_key: 'img_1' },
            { message_id: 'om_1', file_key: 'img_2' },
        ]);
    });

    it('每个文件一条 POST，打到文件管线', async () => {
        const { deps, sent } = recorder();

        await cacheLarkAttachments(deps, oneFile(), recorded(openGroup), event);

        expect(sent).toEqual([
            {
                path: '/api/file-pipeline/process',
                body: { message_id: 'om_1', file_key: 'file_1' },
                headers: { Authorization: 'Bearer inner-secret', 'X-App-Name': 'chiwei' },
            },
        ]);
    });

    // body 里的 message_id 是**飞书裸 om_id**，不是 common_message_id：tool-service 拿它
    // 去飞书开放平台取这条消息的资源。换成公共层 id 之后每一次下载都会 404，而管线
    // 逐条吞错，入站照常。
    it('body 带的是飞书裸 om_id，不是公共层消息 id', async () => {
        const { deps, sent } = recorder();

        await cacheLarkAttachments(deps, oneFile(), recorded(openGroup), event);

        expect(sent[0]!.body.message_id).toBe('om_1');
        expect(sent[0]!.body.message_id).not.toBe('cm_1');
    });

    it('两条轨互不相干：一条消息同时带图片和文件时各走各的端点', async () => {
        const { deps, sent } = recorder();

        await cacheLarkAttachments(
            deps,
            carrying(
                { type: 'image', value: 'img_1' },
                { type: 'file', value: 'file_1', meta: { file_name: '书.epub' } },
            ),
            recorded(openGroup),
            event,
        );

        expect(sent.map((one) => [one.path, one.body.file_key])).toEqual([
            ['/api/image-pipeline/process', 'img_1'],
            ['/api/file-pipeline/process', 'file_1'],
        ]);
    });

    // ---- gate：两个分支各一条（spec Task D 的验收口径）----

    it('群没开"所有人可下载"时，两条轨一条都不发', async () => {
        const { deps, sent } = recorder();

        await cacheLarkAttachments(
            deps,
            carrying(
                { type: 'image', value: 'img_1' },
                { type: 'file', value: 'file_1', meta: {} },
            ),
            recorded(closedGroup),
            event,
        );

        expect(sent).toEqual([]);
    });

    it('私聊没有群资料这一行，一律允许', async () => {
        const { deps, sent } = recorder();

        await cacheLarkAttachments(deps, oneFile(), recorded(null), event);

        expect(sent.map((one) => one.body.file_key)).toEqual(['file_1']);
    });

    it('这条消息没带附件时一条都不发', async () => {
        const { deps, sent } = recorder();

        await cacheLarkAttachments(deps, reading('text', { text: 'hi' }), recorded(openGroup), event);

        expect(sent).toEqual([]);
    });

    // 拆分前图片轨是**一次性全部发出**（`for` 里不 await，逐条挂 `.catch`）。逐条等前
    // 一条完成是行为变更，而且是有代价的那种：客户端超时 30s，首图卡住时后面几张连请求
    // 都还没发出去，这段时间里一次部署就把它们全带走了。
    it('同一条消息里的附件一次性全部发出，不为前一条等待', async () => {
        const tried: string[] = [];
        let letGo = () => {};
        const blocked = new Promise<void>((resolve) => {
            letGo = resolve;
        });
        const { deps } = recorder({
            post: async (_path, body) => {
                tried.push(body.file_key);
                await blocked;
            },
        });

        const done = cacheLarkAttachments(deps, twoImages(), recorded(openGroup), event);
        await Bun.sleep(0);
        // 第一条还卡在那儿，第二条已经发出去了
        expect(tried).toEqual(['img_1', 'img_2']);

        letGo();
        await done;
    });

    it('文件轨不等图片轨（两条轨拆分前就是各走各的）', async () => {
        const tried: string[] = [];
        let letGo = () => {};
        const blocked = new Promise<void>((resolve) => {
            letGo = resolve;
        });
        const { deps } = recorder({
            post: async (_path, body) => {
                tried.push(body.file_key);
                await blocked;
            },
        });

        const done = cacheLarkAttachments(
            deps,
            carrying(
                { type: 'image', value: 'img_1' },
                { type: 'file', value: 'file_1', meta: {} },
            ),
            recorded(openGroup),
            event,
        );
        await Bun.sleep(0);
        expect(tried).toEqual(['img_1', 'file_1']);

        letGo();
        await done;
    });

    // ---- 失败不外溢 ----

    it('一条 POST 失败不打断其余，也绝不抛进入站', async () => {
        const tried: string[] = [];
        const { deps } = recorder({
            post: async (_path, body) => {
                tried.push(body.file_key);
                if (body.file_key === 'img_1') throw new Error('tool-service 500');
            },
        });

        // 不抛
        await cacheLarkAttachments(deps, twoImages(), recorded(openGroup), event);

        expect(tried).toEqual(['img_1', 'img_2']);
    });

    it('图片轨整条炸掉也不影响文件轨', async () => {
        const tried: string[] = [];
        const { deps } = recorder({
            post: async (path, body) => {
                tried.push(body.file_key);
                if (path === '/api/image-pipeline/process') throw new Error('识图管线挂了');
            },
        });

        await cacheLarkAttachments(
            deps,
            carrying(
                { type: 'image', value: 'img_1' },
                { type: 'file', value: 'file_1', meta: {} },
            ),
            recorded(openGroup),
            event,
        );

        expect(tried).toEqual(['img_1', 'file_1']);
    });

    // ---- header ----

    it('带上内网口令和处理这条消息的 bot', async () => {
        const { deps, sent } = recorder();

        await cacheLarkAttachments(deps, oneFile(), recorded(openGroup), { ...event, botName: 'tool' });

        expect(sent[0]!.headers.Authorization).toBe('Bearer inner-secret');
        expect(sent[0]!.headers['X-App-Name']).toBe('tool');
    });

    // 泳道上文件轨显式钉 pod 泳道、图片轨不钉 —— 这是**拆分前就有的不一致**，照搬。
    // 拆分前的理由（file-pipeline.ts 的文件头）：dev-bot 的 webhook 经 gateway 打进来
    // 时不带 x-ctx-lane，请求作用域的 lane 是空的，于是这条后台调用被路由到 prod
    // tool-service。图片轨没做这件事，所以泳道上的图片仍然由 prod tool-service 处理。
    // 差异写在报告里，本批不修（改它是行为变更，会让"行为与拆分前一致"这个判据失效）。
    it('上下文没有泳道时，文件轨拿 pod 泳道兜底、图片轨不兜（照搬拆分前的不一致）', async () => {
        const { deps, sent } = recorder({ lane: 'coe-lark' });

        await cacheLarkAttachments(
            deps,
            carrying(
                { type: 'image', value: 'img_1' },
                { type: 'file', value: 'file_1', meta: {} },
            ),
            recorded(openGroup),
            event,
        );

        const [image, file] = sent;
        expect(image!.headers['x-ctx-lane']).toBeUndefined();
        expect(file!.headers['x-ctx-lane']).toBe('coe-lark');
    });

    it('prod 没有泳道，两条轨都不带 x-ctx-lane', async () => {
        const { deps, sent } = recorder({ lane: undefined });

        await cacheLarkAttachments(
            deps,
            carrying(
                { type: 'image', value: 'img_1' },
                { type: 'file', value: 'file_1', meta: {} },
            ),
            recorded(openGroup),
            event,
        );

        expect(sent.every((one) => one.headers['x-ctx-lane'] === undefined)).toBe(true);
    });
});

describe('assembleLarkAttachments', () => {
    // 入站挂的是这一步。它**必须**当场把控制权还回去：返回 Promise 的话，调用点写不写
    // `await` 就成了每次都要重新做对的选择题，而做错那次没有任何症状 —— 只是每条带附件
    // 的消息都慢一个 HTTP 往返，赤尾回话变迟钝。
    it('调用方立刻拿回控制权，而请求已经发出去了', async () => {
        const tried: string[] = [];
        let letGo = () => {};
        const blocked = new Promise<void>((resolve) => {
            letGo = resolve;
        });
        const cache = assembleLarkAttachments({
            post: async (_path, body) => {
                tried.push(body.file_key);
                await blocked;
            },
            innerSecret: 'inner-secret',
            lane: undefined,
        });

        // 返回的不是 Promise —— 拿不到就 await 不了
        expect(cache(twoImages(), recorded(openGroup), event)).toBeUndefined();
        // 两条都发出去了，而且调用方一条也没等
        expect(tried).toEqual(['img_1', 'img_2']);

        letGo();
        await Bun.sleep(0);
    });
});
