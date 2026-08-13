// 入站附件的字节缓存。
//
//     这条消息正文里的 image_key / file_key ──▶ tool-service ──▶ 下载、原样存进对象存储
//
// 消息本身在投影那一步就已经**无条件**落进 common_message.content 了（那才是 source of
// truth）。这里做的是另一件事：把飞书那侧只能凭 key 临时取到的字节，趁着还取得到的时候
// 存下来。存不下来不影响这条消息存在，所以整条链路是旁路。
//
// ## 为什么这一层特别容易被漏掉
//
// 它没有任何成功信号，失败也没有：POST 失败逐条吞掉，入站照常走完、赤尾照常回话。漏掉
// 它的症状要等到几天后有人问「赤尾怎么读不到我发的书」—— 对象存储里 `files/<file_key>`
// 一直是空的，而依赖它的能力（读小说、识图）稳定读不到东西。所以 gate 的两个分支和
// "失败不外溢"都有测试正面钉住，不靠"看着对"。
//
// ## 两条轨，一份实现
//
// 拆分前这是两个几乎逐字相同的文件（image-pipeline.ts / file-pipeline.ts），差别只有
// 端点、key 从哪来、以及下面那条 `podLaneFallback`。合成一份之后差异集中在 TRACKS 那张
// 表上，加第三条轨（真有的话）是加一行。
//
// ## gate：群没开"所有人可下载"就整条跳过
//
// 判据与投影写进 `common_conversation.attachment_policy.download_allowed` 的是**同一个
// 函数**（见 projection/tables.ts 的 larkDownloadAllowed）—— 两处各写一遍的话，下游按
// attachment_policy 判断"这条消息的图能不能取"，而我们按另一套口径已经把它取下来了。
//
// 事实直接取投影顺路读到的那一行（LarkCommandFacts.groupChat），不再查一次库：拆分前
// 是 `message.allowDownloadResource()` 读 metadata 里同一行群资料，等价。
//
// ## 挂载点与拆分前的两处差别（继承自 Task B 的形状，登记在案）
//
// 拆分前它挂在 om_id 锁**里面**、写消息事务**之前**；本服务挂在 receive-message.ts 的
// 投影与规则之间，而投影内部已经写完库并放掉了锁。所以这两条不是等价物：
//
//   1. **落库失败时不再发。** 拆分前已经发出去了。这是收紧，而且与本服务已经定下的
//      "落账失败则规则根本不跑"同向（见 receive-message.ts 文件头）。
//   2. **同群多个 bot 在锁外并发发。** 拆分前它们被 om_id 锁串成先后。**条数不变**
//      （每个 bot 各发一遍，拆分前也是），变的只是同时还是先后。
//
// 不把挂载点挪回锁里：那等于把一次外部 HTTP 往返塞进按 om_id 的锁和写库事务旁边，锁的
// 持有时间从"写两行"变成"等 tool-service"，代价比这两条差别大得多。

import type { LarkEvent } from './ingress/lark-event';
import { larkFileKeys, larkImageKeys, type LarkContentPart } from './message/lark-content';
import type { LarkMessageReading } from './message/read-message-event';
import type { LarkRecordedInbound } from './projection/inbound-projection';
import { larkDownloadAllowed } from './projection/tables';

/** 一条投给 tool-service 的请求。真身是 laneRouter 给 tool-service 建的 axios 客户端。 */
export type LarkAttachmentPost = (
    path: string,
    body: { message_id: string; file_key: string },
    headers: Record<string, string>,
) => Promise<unknown>;

/** 一条轨：正文里的哪一类 key，交给 tool-service 的哪个管线。 */
export interface LarkAttachmentTrack {
    /** 日志里的标识。 */
    readonly name: string;
    /** tool-service 的端点。**跨服务契约**，写错了发出去是 404，而 404 被逐条吞掉。 */
    readonly path: string;
    readonly keysOf: (parts: LarkContentPart[]) => string[];
    /**
     * 请求上下文里没有泳道时，拿 pod 的静态泳道补上 `x-ctx-lane`。
     *
     * **两条轨在这里不一致，而这是拆分前就有的形态，本批照搬。** 文件轨补、图片轨不补。
     *
     * 它是**兜底不是钉死**：laneRouter 的拦截器会用非空的上下文 lane 覆盖同名 header
     * （见 packages/ts-shared/src/lane-router/index.ts 那个 request 拦截器），所以上下文
     * 有 lane 时两条轨走的是同一条路。
     *
     * 真正分岔的只有**上下文里没有 lane 的那条路**：webhook / 长连直接打进泳道时，
     * gateway 不注 `x-ctx-lane`，请求作用域的 lane 是空的（泳道信封那条路不同 ——
     * ingress/lark-event.ts 会把信封里的 lane 注进上下文，两条轨都拿得到）。这时文件轨
     * 打本泳道的 tool-service、图片轨打 prod 的。拆分前文件轨那条注释记的就是这个坑：
     * 当时 prod 没有文件管线端点，表现是 404、文件永远缓存不进对象存储。
     *
     * 不在这批统一：改它是行为变更，会让"行为与拆分前一致"这个唯一的验收判据失效。
     */
    readonly podLaneFallback: boolean;
}

/** 先图片后文件，与拆分前调用点的两行顺序一致。 */
export const LARK_ATTACHMENT_TRACKS: readonly LarkAttachmentTrack[] = [
    {
        name: 'image',
        path: '/api/image-pipeline/process',
        keysOf: larkImageKeys,
        podLaneFallback: false,
    },
    {
        name: 'file',
        path: '/api/file-pipeline/process',
        keysOf: larkFileKeys,
        podLaneFallback: true,
    },
];

export interface LarkAttachmentDeps {
    post: LarkAttachmentPost;
    /** 调 tool-service 的内网口令。缺了发出的是 `Bearer undefined`，对面 401。 */
    innerSecret: string | undefined;
    /** 本 pod 的静态泳道（`getLane()`）。prod 是 undefined。 */
    lane: string | undefined;
}

/**
 * 把这条消息带的附件交给 tool-service。**永远不抛**：每条 POST 各自吞错，一条失败不影响
 * 其余，更不会冒泡进入站流程。返回的 Promise 在全部落定后 resolve（测试用得上）。
 *
 * **一次性全部发出，谁也不等谁**，照拆分前图片轨那个不 await 的 `for`。改成逐条等前一条
 * 完成是有代价的：客户端超时 30s，首图卡住时后面几张连请求都还没发出去，而这段时间里
 * 一次部署就把它们全带走了。
 */
export function cacheLarkAttachments(
    deps: LarkAttachmentDeps,
    reading: LarkMessageReading,
    recorded: LarkRecordedInbound,
    event: LarkEvent,
): Promise<void> {
    const allowed = larkDownloadAllowed(recorded.commands.groupChat);
    const sent: Promise<void>[] = [];

    for (const track of LARK_ATTACHMENT_TRACKS) {
        const keys = track.keysOf(reading.content);
        if (keys.length === 0) continue;
        if (!allowed) {
            // 说一声。没有这条日志的话，"这个群的附件从来没进过对象存储"与"这个群
            // 从来没人发过附件"在外面看是同一个现象。
            console.info(
                `[lark-attachments] skipping ${keys.length} ${track.name}(s) of ` +
                    `${reading.message.messageId}: chat ${reading.message.chatId} does not ` +
                    'let all members download resources',
            );
            continue;
        }

        for (const key of keys) sent.push(hand(deps, track, key, reading, event));
    }

    return Promise.all(sent).then(() => undefined);
}

/** 一条 POST。吞掉自己的错，所以调用方拿到的 Promise 永远不 reject。 */
async function hand(
    deps: LarkAttachmentDeps,
    track: LarkAttachmentTrack,
    key: string,
    reading: LarkMessageReading,
    event: LarkEvent,
): Promise<void> {
    try {
        await deps.post(
            track.path,
            { message_id: reading.message.messageId, file_key: key },
            {
                Authorization: `Bearer ${deps.innerSecret}`,
                'X-App-Name': event.botName,
                ...(track.podLaneFallback && deps.lane ? { 'x-ctx-lane': deps.lane } : {}),
            },
        );
    } catch (error) {
        console.error(
            `[lark-attachments] could not hand the ${track.name} ${key} of ` +
                `${reading.message.messageId} to tool-service:`,
            error,
        );
    }
}

/** 入站流程上挂的那一步。签名是 `void` —— 这是契约的一部分，见下。 */
export type LarkAttachmentCache = (
    reading: LarkMessageReading,
    recorded: LarkRecordedInbound,
    event: LarkEvent,
) => void;

/**
 * 把依赖绑上，得到入站流程上挂的那一步。
 *
 * **返回 void 而不是 Promise 是刻意的**：入站绝不为一次 tool-service 往返等待。返回
 * Promise 的话，调用点写不写 `await` 就成了一个每次都要重新做对的选择题，而写错的那
 * 一次没有任何症状 —— 只是每条带附件的消息都慢一个 HTTP 往返，赤尾回话变迟钝。
 */
export function assembleLarkAttachments(deps: LarkAttachmentDeps): LarkAttachmentCache {
    return (reading, recorded, event) => {
        void cacheLarkAttachments(deps, reading, recorded, event);
    };
}
