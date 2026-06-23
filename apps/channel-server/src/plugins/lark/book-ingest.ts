/**
 * 书接入分流 + 转发（读小说 Task 1，channel-server 侧）。
 *
 * 真人在飞书**私聊**把一个 txt/epub 文件发给赤尾时，这条文件消息不走文本 chat 链路，
 * 而是走一条专门的书接入路径：下载飞书文件 → base64 → POST 给 agent-service 的
 * `/api/internal/book/ingest`，由 agent-service 解析入库（**只存书、不投信箱**——她靠跟你
 * 的真实对话知道这本书）。解析失败时 agent-service 回结构化失败，这里把提示回给真人（不静默吞）。
 *
 * 为什么只 p2p：书是「你私下推荐给她读」的，群里发文件不当书（避免误吞群文件）。
 * 为什么独立模块：分流判定（哪些文件当书）+ 下载转发是可单测的纯逻辑 + 注入式 IO，
 * 不塞进已经很长的 handlers.ts。
 */

import type { ContentItem } from '@core/channels/contracts';
import { Readable } from 'node:stream';

// 重 IO 模块（飞书下载 / lane-router 调 agent-service / 出站回复）只在生产 deps 工厂
// makeBookForwardDeps 里**动态 import**，不在模块顶层静态 import。原因：静态 import 会
// 把这些模块（及其 @inner/shared / @larksuiteoapi 依赖链）拖进任何 import 本模块的测试，
// 单测要么解析不了、要么得 mock.module 它们——而 bun 的 mock.module 是全局的、会泄漏到
// 后续测试文件。动态 import 让纯分流逻辑（selectBookFile / forwardBookFile）可被单测直接
// 导入、零 mock，生产工厂用时才加载真实 IO。

const BOOK_EXTENSIONS = ['.txt', '.epub'];

/** 文件名是不是一本书（txt / epub，大小写不敏感）。 */
export function isBookFilename(fileName: string | undefined): boolean {
    if (!fileName) return false;
    const lower = fileName.toLowerCase();
    return BOOK_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

export interface SelectedBookFile {
    fileKey: string;
    fileName: string;
}

/**
 * 分流判定：这条消息是不是「p2p 私聊里的 txt/epub 文件」。是则返回该文件的 key + 名，
 * 否则返回 null（继续走现有处理 / 被忽略）。
 *
 * 只认 `kind: 'file'` 且 `meta.lark_type === 'file'`（真正的文件消息，排除视频 media）、
 * 文件名是 txt/epub、且会话是 direct（私聊）。
 */
export function selectBookFile(
    content: ContentItem[],
    scope: 'direct' | 'group' | string,
): SelectedBookFile | null {
    if (scope !== 'direct') return null;
    for (const item of content) {
        if (item.kind !== 'file') continue;
        const meta = (item.meta ?? {}) as Record<string, unknown>;
        if (meta.lark_type !== 'file') continue; // 排除 media（视频）等
        const fileName = typeof meta.file_name === 'string' ? meta.file_name : undefined;
        if (!isBookFilename(fileName)) continue;
        return { fileKey: item.key, fileName: fileName! };
    }
    return null;
}

export interface BookForwardInput {
    lane: string;
    botName: string;
    messageId: string;
    fileKey: string;
    fileName: string;
}

export interface IngestHttpResult {
    ok: boolean; // HTTP 层是否 2xx
    status: number;
    json: { ok?: boolean; reason?: string; book_id?: string; title?: string } | null;
}

/** 注入式 IO 边界，便于单测 mock 飞书下载 / agent-service 调用 / 回复真人。 */
export interface BookForwardDeps {
    downloadFile: (messageId: string, fileKey: string) => Promise<Buffer>;
    postIngest: (path: string, body: Record<string, unknown>) => Promise<IngestHttpResult>;
    replyToUser: (msg: string) => Promise<void>;
}

const FAILURE_HINT = '这个文件我没能读成一本书，要不换个 txt 或 epub 再发一次？';

/**
 * 下载飞书文件 → base64 → POST 给 agent-service 书接入路径；失败回真人一条提示。
 *
 * 任一环节失败（下载抛错 / HTTP 非 2xx / agent-service 回 ok=false）都回真人 FAILURE_HINT，
 * 绝不静默吞、也不让异常冒泡炸掉入站流程（书接入是入站的一条旁路、失败不该拖垮别的处理）。
 */
export async function forwardBookFile(
    input: BookForwardInput,
    deps: BookForwardDeps,
): Promise<void> {
    let buffer: Buffer;
    try {
        buffer = await deps.downloadFile(input.messageId, input.fileKey);
    } catch (err) {
        console.error(
            `[book-ingest] download failed: message=${input.messageId} ` +
                `file=${input.fileKey}: ${(err as Error).message}`,
        );
        await safeReply(deps, FAILURE_HINT);
        return;
    }

    let result: IngestHttpResult;
    try {
        result = await deps.postIngest('/api/internal/book/ingest', {
            lane: input.lane,
            bot_name: input.botName,
            filename: input.fileName,
            file_b64: buffer.toString('base64'),
        });
    } catch (err) {
        console.error(`[book-ingest] agent-service ingest call failed: ${(err as Error).message}`);
        await safeReply(deps, FAILURE_HINT);
        return;
    }

    if (!result.ok || !result.json || result.json.ok !== true) {
        const reason = result.json?.reason;
        console.warn(
            `[book-ingest] ingest reported failure: status=${result.status} ` +
                `reason=${reason ?? '(none)'}`,
        );
        await safeReply(deps, reason ? `${reason}` : FAILURE_HINT);
        return;
    }

    console.info(
        `[book-ingest] book ingested: book_id=${result.json.book_id} ` +
            `title=${result.json.title} lane=${input.lane}`,
    );
}

async function safeReply(deps: BookForwardDeps, msg: string): Promise<void> {
    try {
        await deps.replyToUser(msg);
    } catch (err) {
        console.warn(`[book-ingest] reply to user failed: ${(err as Error).message}`);
    }
}

/** 把飞书 SDK 下载响应（含 getReadableStream）读成 Buffer。 */
async function streamToBuffer(stream: Readable): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of stream) {
        chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
}

/** 生产 deps：真实飞书下载 + lane-router 调 agent-service + 回飞书消息（重 IO 动态 import）。 */
export function makeBookForwardDeps(messageIdForReply: string): BookForwardDeps {
    return {
        downloadFile: async (messageId, fileKey) => {
            const { downloadResource } = await import('@lark-client');
            const resp = await downloadResource(messageId, fileKey, 'file');
            return streamToBuffer(resp.getReadableStream());
        },
        postIngest: async (path, body) => {
            const { laneRouter } = await import('@infrastructure/lane-router');
            const resp = await laneRouter.fetch('agent-service', path, {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify(body),
            });
            let json: IngestHttpResult['json'] = null;
            try {
                json = (await resp.json()) as IngestHttpResult['json'];
            } catch {
                json = null;
            }
            return { ok: resp.ok, status: resp.status, json };
        },
        replyToUser: async (msg) => {
            const { replyMessage } = await import('@lark/basic/message');
            await replyMessage(messageIdForReply, msg);
        },
    };
}
