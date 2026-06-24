/**
 * 文件入轨（读小说 Task 1，channel-server 侧）。
 *
 * 真人发来的任何文件，先作为一条普通文件内容项落进 common_message.content（source of
 * truth，由入站链路无条件完成）；这里只做和图片同款的 best-effort 字节缓存：fire-and-forget
 * POST 给 tool-service 的 /api/file-pipeline/process，由它下载（type=file）+ 原样存 TOS。
 * 缓存失败不影响这条消息已经存在，绝不 gate 入站、绝不向真人说话。
 *
 * 镜像 image-pipeline.ts 的 enqueueLarkImagePipeline；差别只在打的是文件管线端点、
 * 文件 key 来源是 message.fileKeys()（只取真正的文件、不含视频 media / 图片）。
 */

import type { Message } from 'core/models/message';
import { laneRouter } from '@infrastructure/lane-router';

type Poster = (
    path: string,
    body: Record<string, unknown>,
    opts: { headers: Record<string, unknown> },
) => Promise<unknown>;

/**
 * 可单测核心：对每个 file key fire-and-forget 一条 POST，逐条吞错（best-effort，
 * 绝不把异常冒泡进入站流程）。注入 post 便于单测。
 */
export async function enqueueFilePipelinePosts(args: {
    messageId: string;
    fileKeys: string[];
    botName: string | undefined;
    innerSecret: string | undefined;
    post: Poster;
}): Promise<void> {
    const { messageId, fileKeys, botName, innerSecret, post } = args;
    for (const fileKey of fileKeys) {
        try {
            await post(
                '/api/file-pipeline/process',
                { message_id: messageId, file_key: fileKey },
                {
                    headers: {
                        Authorization: `Bearer ${innerSecret}`,
                        'X-App-Name': botName,
                    },
                },
            );
        } catch (err) {
            console.error('Error in file pipeline enqueue:', err);
        }
    }
}

/**
 * 生产入口：把这条消息里的文件 key 投给 tool-service 文件管线（fire-and-forget）。
 * 与 enqueueLarkImagePipeline 对称：不 await、不阻塞入站，缓存失败只记日志。
 */
export function enqueueLarkFilePipeline(message: Message, botName: string | undefined): void {
    if (!message.allowDownloadResource()) return;
    const fileKeys = message.fileKeys();
    if (fileKeys.length === 0) return;

    const toolClient = laneRouter.createClient('tool-service');
    void enqueueFilePipelinePosts({
        messageId: message.messageId,
        fileKeys,
        botName,
        innerSecret: process.env.INNER_HTTP_SECRET,
        post: (path, body, opts) =>
            toolClient.post(path, body, { headers: opts.headers as Record<string, string> }),
    });
}
