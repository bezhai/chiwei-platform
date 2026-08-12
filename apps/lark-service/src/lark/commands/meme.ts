// 「Meme」：@ 一下赤尾，第一个词命中某个表情包模板的关键词，就现做一张发回来。
//
//     @bot 摸 赤尾 circle=true
//        │
//        ├─ 谓词（async）：问 meme 服务有哪些模板，第一个词是不是某个模板的关键词
//        └─ handler：切文本与参数 → 取消息里的图 → 让 meme 服务做 → 传飞书 → 回图
//
// ## 它是清单里唯一一条带 async 谓词的指令
//
// 因为"这句话是不是 meme"必须先问过 meme 服务才知道。谓词不通过就是**不命中**，消息
// 继续往后走 —— 所以它虽然近似 catch-all（同步谓词只有 `NeedRobotMention`），却不会把
// 后面的规则吃掉。反过来，它必须排在清单**最后**：排到那几条 `EqualText` 前面，「余额」
// 「帮助」这些词只要碰巧不是 meme 关键词就还好，一旦撞上就被它整条吃掉。
//
// ## 谓词和 handler 用两套切法，这是上游的形态不是笔误
//
//   谓词    `clearText.split(' ')` 取第一个非空词 —— 不认引号
//   handler `parseCommandText`     认引号和反斜杠
//
// 于是 `"摸" 赤尾` 这种写法谓词认不出来（它看到的第一个词是 `"摸"`），整条不命中。照搬。
//
// ## 图片闸的判据是 `not_anyone`，不是 `all_members`
//
// 投影那侧判"这个会话准不准取附件"用的是 `download_has_permission_setting === 'all_members'`
// （见 ../projection/tables.ts 的 larkDownloadAllowed），而这里照搬上游的
// `== 'not_anyone'`。**两者不等价**：只有管理员能下载的群（`only_manager`）在投影眼里
// 是禁止、在这条闸眼里是允许。不要顺手统一 —— 统一之后那些群的 meme 会突然用不了，
// 而没有任何报错说得清为什么。
//
// 另外这条闸是三个条件同时成立才拦：模板吃图、消息真的带了图、群禁止任何人下载。少一个
// 就照做。
//
// ## 出错就对着用户说一句，绝不静默
//
// 与拆分前一致：整段包在 try 里，失败翻成一句话（**不进话题**，上游那句 replyMessage 没
// 传第三个参数）。谓词那一侧相反 —— 它自己吞掉异常返回 false，于是 meme 服务挂了的时候
// 整条指令只是不命中，不会对每条 @ 消息都回一句报错。

import type { Readable } from 'node:stream';
import { NeedRobotMention } from '@inner/shared/rules';
import type { RuleMessage } from '@inner/shared/rules';

import type { LarkCommandContext } from '../rules/command-context';
import type { LarkCommand, LarkCommandDeps } from '../rules/commands';
import type { LarkMeme } from './memes';

/** 群那一列取这个值时禁止任何人下载消息里的图。**只有这一个值算禁止**，见文件头。 */
const NO_ONE_MAY_DOWNLOAD = 'not_anyone';

const UPLOAD_FAILED = '上传图片失败';
const NEEDS_IMAGES =
    '该类meme需要获取消息中图片, 但当前群聊不允许下载消息中图片, 请在其他群聊或私聊中使用';
const UNKNOWN = '生成表情包失败，原因未知';

/**
 * 按空格切，引号里的空格算文本，反斜杠转义下一个字符。逐字照搬上游。
 *
 * 引号**成对与否不检查**：`摸 "赤尾` 会把后面整段当成一项。这是上游的形态。
 */
export function parseCommandText(text: string): string[] {
    const result: string[] = [];
    let current = '';
    let inQuotes = false;
    let escapeNext = false;

    for (const char of text) {
        if (escapeNext) {
            current += char;
            escapeNext = false;
            continue;
        }
        if (char === '\\') {
            escapeNext = true;
            continue;
        }
        if (char === '"' || char === "'") {
            inQuotes = !inQuotes;
            continue;
        }
        if (char === ' ' && !inQuotes) {
            if (current.length > 0) {
                result.push(current);
                current = '';
            }
            continue;
        }
        current += char;
    }

    if (current.length > 0) result.push(current);
    return result;
}

function memeFor(templates: LarkMeme[], keyword: string | undefined): LarkMeme | undefined {
    return templates.find((template) => template.keywords.includes(keyword as string));
}

export function memeCommand(deps: LarkCommandDeps): LarkCommand {
    return (context) => ({
        rules: [NeedRobotMention],
        async_rules: [(message) => looksLikeMeme(deps, message)],
        comment: 'Meme',
        category: 'utility',
        handler: async (message) => {
            try {
                await makeMeme(deps, context, message);
            } catch (error) {
                await apologise(deps, context, error);
            }
        },
    });
}

/**
 * 这句话是不是某个表情包的关键词。
 *
 * **自己吞异常返回 false**：meme 服务挂了的时候整条指令只是不命中，消息照常往后走。
 * 让它抛出去的话，引擎会把整条消息收敛成 rule_error —— 群里每一条 @ 赤尾的消息都不再
 * 有回复，而症状跟"赤尾哑了"完全一样。
 */
async function looksLikeMeme(deps: LarkCommandDeps, message: RuleMessage): Promise<boolean> {
    try {
        const templates = await deps.memes.templates();
        // 谓词这一侧按空格切、不认引号。与 handler 那侧的差别见文件头。
        const first = message
            .clearText()
            .split(' ')
            .filter((word) => word.length > 0)[0];
        return memeFor(templates, first) !== undefined;
    } catch (error) {
        console.error('[lark-meme] could not ask the meme service what it has:', error);
        return false;
    }
}

async function makeMeme(
    deps: LarkCommandDeps,
    context: LarkCommandContext,
    message: RuleMessage,
): Promise<void> {
    const templates = await deps.memes.templates();
    const parts = parseCommandText(message.clearText());
    const meme = memeFor(templates, parts[0]);
    if (!meme) throw new Error('Meme not found');

    const imageKeys = message.imageKeys();
    if (
        (meme.params_type.max_images || 0) > 0 &&
        imageKeys.length > 0 &&
        context.groupChat?.download_has_permission_setting === NO_ONE_MAY_DOWNLOAD
    ) {
        throw new Error(NEEDS_IMAGES);
    }

    // 带等号的词是参数，其余是印在图上的文字。`a=b=c` 按第一个等号切（split 之后只取
    // 前两项），与上游一致。
    const args: Record<string, string> = {};
    const texts: string[] = [];
    for (const part of parts.slice(1)) {
        if (part.includes('=')) {
            const [key, value] = part.split('=');
            args[key!] = value!;
        } else {
            texts.push(part);
        }
    }

    // 逐张取，不并发 —— 与上游一致。一条消息里的图不会多到值得并发。
    const images: Readable[] = [];
    for (const imageKey of imageKeys) {
        images.push(await deps.api.downloadResource(context.message.messageId, imageKey, 'image'));
    }

    const bytes = await deps.memes.render(meme.key, texts, images, args);
    const uploaded = await deps.api.uploadImage(bytes);
    if (!uploaded) throw new Error(UPLOAD_FAILED);

    await deps.api.replyImage(context.message.messageId, uploaded);
}

/**
 * 把失败翻成一句话。meme 服务给的说法（"文字太长了"这类）原样转达 —— 它是写给用户看的。
 *
 * **不进话题**：上游那句 replyMessage 没传第三个参数。自己再失败也不外溢。
 */
async function apologise(
    deps: LarkCommandDeps,
    context: LarkCommandContext,
    error: unknown,
): Promise<void> {
    const said = (error instanceof Error && error.message) || UNKNOWN;
    console.error('[lark-meme] could not make the meme:', error);
    try {
        await deps.api.replyText(context.message.messageId, said, false);
    } catch (replyError) {
        console.error('[lark-meme] could not even tell the user it failed:', replyError);
    }
}
