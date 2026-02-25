import { AxiosResponse } from 'axios';
import { Message } from '@core/models/message';
import { Meme } from 'types/meme';
import { Readable } from 'node:stream';
import { uploadImage, downloadResource } from '@lark-client';
import FormData from 'form-data';
import { replyImage, replyMessage } from '@lark/basic/message';
import { GroupChatInfoRepository } from '@infrastructure/dal/repositories/repositories';
import { cache } from '@cache/cache-decorator';
import http from '@http/client';

// 缓存过期时间（10分钟）
const MEME_CACHE_EXPIRY = 10 * 60;

// 获取表情包列表服务类
class MemeService {
    /**
     * 获取表情包列表，优先从缓存获取
     */
    @cache({ type: 'redis', ttl: MEME_CACHE_EXPIRY })
    static async getMemeList(): Promise<Meme[]> {
        try {
            // 直接从API获取
            const response: AxiosResponse<Meme[]> = await http.get(
                `${process.env.MEME_HOST}:${process.env.MEME_PORT}/memes/list`,
            );
            return response.data;
        } catch (error) {
            console.error('获取表情包列表失败:', error);
            throw error;
        }
    }
}

export async function checkMeme(message: Message): Promise<boolean> {
    try {
        const memeList = await MemeService.getMemeList();

        const clearText = message.clearText();

        const textKeyword = clearText.split(' ').filter((keyword) => keyword.length > 0)[0];

        const meme = memeList.find((meme) => {
            return meme.keywords.includes(textKeyword);
        });

        return meme !== undefined;
    } catch (error: any) {
        console.error('Error in req:', error);
        return false;
    }
}

/**
 * 生成表情包图片并上传
 * @param texts 文本数组
 * @param name 表情包名称
 * @param images 图片数据
 * @param args 额外参数
 * @returns 图片键值
 */
async function generateMemeImage(
    texts: string[],
    name: string,
    images: Readable[] = [],
    args: Record<string, any> = {},
): Promise<string> {
    try {
        // 准备请求体 - 使用FormData处理multipart/form-data请求
        const formData = new FormData();

        // 添加文本
        for (const text of texts) {
            formData.append('texts', text);
        }

        // 添加参数
        if (Object.keys(args).length > 0) {
            formData.append('args', JSON.stringify(args));
        }

        // 添加图片
        for (let i = 0; i < images.length; i++) {
            formData.append('images', images[i], `${i}.jpg`);
        }

        // 发送请求到表情包服务
        const response = await http.post(
            `${process.env.MEME_HOST}:${process.env.MEME_PORT}/memes/${name}/`,
            formData,
            {
                headers: {
                    ...formData.getHeaders(),
                },
                responseType: 'arraybuffer',
                validateStatus: function (_) {
                    return true; // 允许所有状态码，不抛出错误
                },
            },
        );

        // 检查状态码
        if (response.status !== 200) {
            // 尝试解析响应中的detail字段
            let errorMessage = '生成表情包失败';
            try {
                const errorResponse = JSON.parse(Buffer.from(response.data).toString());
                if (errorResponse.detail) {
                    errorMessage = errorResponse.detail;
                }
            } catch (parseError) {
                console.error('解析错误响应失败:', parseError);
            }
            throw new Error(errorMessage);
        }

        // 将响应数据转换为可读流
        const buffer = Buffer.from(response.data);
        const readable = new Readable();
        readable.push(buffer);
        readable.push(null);

        // 上传图片
        const result = await uploadImage(readable);
        if (result && result.image_key) {
            return result.image_key;
        }

        throw new Error('上传图片失败');
    } catch (error: any) {
        console.error('生成表情包错误:', error);
        throw new Error(error.message || '生成表情包失败');
    }
}

/**
 * 解析命令行文本，支持引号包裹和转义字符
 * @param text 输入文本
 * @returns 解析后的文本数组
 */
function parseCommandText(text: string): string[] {
    const result: string[] = [];
    let current = '';
    let inQuotes = false;
    let escapeNext = false;

    for (let i = 0; i < text.length; i++) {
        const char = text[i];

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
            if (inQuotes) {
                inQuotes = false;
            } else {
                inQuotes = true;
            }
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

    if (current.length > 0) {
        result.push(current);
    }

    return result;
}

export async function genMeme(message: Message) {
    try {
        const memeList = await MemeService.getMemeList();

        const clearText = message.clearText();

        // 使用新的解析函数
        const textParts = parseCommandText(clearText);
        const textKeyword = textParts[0];

        const meme = memeList.find((meme) => {
            return meme.keywords.includes(textKeyword);
        });

        if (!meme) {
            throw new Error('Meme not found');
        }

        // 如果群聊不允许下载图片, 使用图片的规则需要报错

        const groupInfo = await GroupChatInfoRepository.findOne({
            where: {
                chat_id: message.chatId,
            },
        });

        if (
            (meme.params_type.max_images || 0) > 0 &&
            message.imageKeys().length > 0 &&
            groupInfo?.download_has_permission_setting == 'not_anyone'
        ) {
            throw new Error(
                '该类meme需要获取消息中图片, 但当前群聊不允许下载消息中图片, 请在其他群聊或私聊中使用',
            );
        }

        // 获取消息中的文本和图片
        let rawTexts = textParts.slice(1);
        const args: Record<string, any> = {};
        const texts: string[] = [];

        // 提取参数和纯文本
        for (const text of rawTexts) {
            if (text.includes('=')) {
                const [key, value] = text.split('=');
                args[key] = value;
            } else {
                texts.push(text);
            }
        }

        const messageImages: Readable[] = [];

        for (const image of message.imageKeys()) {
            const imageStream = await downloadResource(message.messageId, image, 'image');
            messageImages.push(imageStream.getReadableStream());
        }

        // 这里可以添加图片提取逻辑，如果需要的话

        // 调用生成表情包函数
        const imageKey = await generateMemeImage(texts, meme.key, messageImages, args);

        // 使用项目中的回复函数发送图片
        await replyImage(message.messageId, imageKey);
    } catch (error: any) {
        console.error('Error in req:', error);
        // 向用户回复错误信息
        await replyMessage(message.messageId, error.message || '生成表情包失败，原因未知');
    }
}
