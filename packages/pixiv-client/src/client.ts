import axios from 'axios';
import { sendAuthenticatedRequest, urlEncode } from './auth';
import {
    PixivClientConfig,
    createDefaultPixivConfig,
    PixivGenericResponse,
    PixivProxyRequestBody,
    PixivAuth,
    BaseResponse,
    ImageStoreDownloadData,
    FollowerInfo,
    FollowerBody,
    AuthorArtworkResponseBody,
    TagArtworkResponseBody,
    IllustrationPageDetail,
    IllustDetail,
} from './types';

/**
 * Pixiv API 客户端
 */
export class PixivClient {
    private config: PixivClientConfig;
    private httpClient: typeof axios;

    constructor(config?: Partial<PixivClientConfig>, httpClient?: typeof axios) {
        this.config = { ...createDefaultPixivConfig(), ...config };
        this.httpClient = httpClient || axios;
    }

    /**
     * 通过代理发送 Pixiv 请求
     */
    async pixivProxy<T>(
        baseUrl: string,
        referer: string,
        params: Record<string, any> = {}
    ): Promise<T> {
        // 如果有查询参数，拼接到 baseUrl
        if (Object.keys(params).length > 0) {
            baseUrl += `?${urlEncode(params)}`;
        }

        const reqBody: PixivProxyRequestBody = {
            url: baseUrl,
            referer: referer,
        };

        const auth = await this.config.authProvider?.();
        if (auth) {
            reqBody.pixiv_auth = auth;
        }

        return await sendAuthenticatedRequest<T>(
            `${this.config.proxyHost}/api/v2/proxy`,
            reqBody,
            this.config.httpSecret,
            this.httpClient
        );
    }

    /**
     * 获取用户指定标签下的关注列表
     */
    async getFollowersByTag(tag: string, userId?: string, signal?: AbortSignal): Promise<FollowerInfo[]> {
        const targetUserId = userId || this.config.defaultUserId || '35384654';
        const pageSize = 24;
        const followers: FollowerInfo[] = [];

        let page = 1;
        let total = 0;

        do {
            signal?.throwIfAborted();
            const pixivUrl = `https://www.pixiv.net/ajax/user/${targetUserId}/following`;
            const referer = `https://www.pixiv.net/users/${targetUserId}/following/${encodeURIComponent(tag)}?p=${page}`;

            const response = await this.pixivProxy<PixivGenericResponse<FollowerBody>>(
                pixivUrl,
                referer,
                {
                    offset: (page - 1) * pageSize,
                    limit: pageSize,
                    rest: 'show',
                    tag: tag,
                    lang: 'zh',
                }
            );

            signal?.throwIfAborted();
            const { error, body } = response;
            if (error || !body) {
                throw new Error(response.message || 'Failed to fetch followers');
            }

            console.log(`Fetched ${body.users.length} followers for page ${page} of total ${body.total}`);

            followers.push(...body.users);
            total = body.total;
            page++;

            // 等待2秒以避免频繁请求
            await new Promise((resolve) => setTimeout(resolve, 2000));
        } while ((page - 1) * pageSize < total);

        return followers;
    }

    /**
     * 根据用户 ID 获取该作者的所有作品 ID
     */
    async getAuthorArtwork(userId: string, version?: string | null): Promise<string[]> {
        const authorUrl = `https://www.pixiv.net/ajax/user/${userId}/profile/all`;
        const referer = `https://www.pixiv.net/users/${userId}`;

        const params: Record<string, any> = { lang: 'zh' };
        if (version) {
            params.version = version;
        } else if (this.config.getVersion) {
            const v = await this.config.getVersion();
            if (v) params.version = v;
        }

        const response = await this.pixivProxy<PixivGenericResponse<AuthorArtworkResponseBody>>(
            authorUrl,
            referer,
            params
        );

        const { error, body } = response;
        if (error || !body) {
            throw new Error(response.message || 'Failed to fetch author artworks');
        }

        return Object.keys(body.illusts);
    }

    /**
     * 根据标签和页码获取作品 ID 列表
     */
    async getTagArtwork(tag: string, page: number, version?: string | null): Promise<string[]> {
        const authorUrl = `https://www.pixiv.net/ajax/search/illustrations/${encodeURIComponent(tag)}`;
        const referer = `https://www.pixiv.net/tags/${encodeURIComponent(tag)}/illustrations?order=popular_d&p=${page}`;

        const params: Record<string, any> = {
            word: tag,
            order: 'popular_d',
            mode: 'all',
            p: page.toString(),
            s_mode: 's_tag',
            type: 'illust_and_ugoira',
            lang: 'zh',
        };

        if (version) {
            params.version = version;
        } else if (this.config.getVersion) {
            const v = await this.config.getVersion();
            if (v) params.version = v;
        }

        const response = await this.pixivProxy<PixivGenericResponse<TagArtworkResponseBody>>(
            authorUrl,
            referer,
            params
        );

        const { error, body } = response;
        if (error || !body || !body.illust) {
            throw new Error(response.message || 'Failed to fetch tag artworks');
        }

        return body.illust.data.map((detail: IllustDetail) => detail.id);
    }

    /**
     * 根据插画 ID 获取插画信息
     */
    async getIllustInfo<T>(illustId: string): Promise<T> {
        const illustUrl = `https://www.pixiv.net/ajax/illust/${encodeURIComponent(illustId)}`;
        const referer = `https://www.pixiv.net/artworks/${encodeURIComponent(illustId)}`;

        const response = await this.pixivProxy<PixivGenericResponse<T>>(illustUrl, referer);

        if (response.error || !response.body) {
            throw new Error(response.message || 'Failed to fetch illustration info');
        }

        return response.body;
    }

    /**
     * 根据插画 ID 获取插画的页面详情
     */
    async getIllustPageDetail(illustId: string): Promise<IllustrationPageDetail[]> {
        const illustUrl = `https://www.pixiv.net/ajax/illust/${encodeURIComponent(illustId)}/pages`;
        const referer = `https://www.pixiv.net/artworks/${encodeURIComponent(illustId)}`;

        const response = await this.pixivProxy<PixivGenericResponse<IllustrationPageDetail[]>>(
            illustUrl,
            referer,
            { lang: 'zh' }
        );

        if (response.error || !response.body) {
            throw new Error(response.message || 'Failed to fetch illustration page details');
        }

        return response.body;
    }

    /**
     * 让 server 下载 Pixiv 图片并上传 OSS，返回实际写入的 OSS key。
     *
     * key 带 server 当天的日期前缀，调用方推算不出来，只能从响应里取。
     * 响应里没有 key 时报错：调用方要拿它写目录记录，没有 key 的成功等于丢图。
     */
    async downloadContent(pixivUrl: string): Promise<string> {
        const reqBody: { pixiv_url: string; pixiv_auth?: PixivAuth } = { pixiv_url: pixivUrl };

        const auth = await this.config.authProvider?.();
        if (auth) {
            reqBody.pixiv_auth = auth;
        }

        const response = await sendAuthenticatedRequest<BaseResponse<ImageStoreDownloadData | null>>(
            `${this.config.proxyHost}/api/v2/image-store/download`,
            reqBody,
            this.config.httpSecret,
            this.httpClient
        );

        if (response.code !== 0) {
            throw new Error(response.msg);
        }

        const key = response.data?.tos_file_name;
        if (typeof key !== 'string' || key === '') {
            throw new Error(`image-store download for ${pixivUrl} returned no tos_file_name`);
        }
        return key;
    }
}

// 默认单例实例
let defaultInstance: PixivClient | null = null;

/**
 * 获取默认 Pixiv 客户端实例
 */
export function getPixivClient(config?: Partial<PixivClientConfig>): PixivClient {
    if (!defaultInstance) {
        defaultInstance = new PixivClient(config);
    }
    return defaultInstance;
}

/**
 * 重置默认 Pixiv 客户端实例
 */
export function resetPixivClient(): void {
    defaultInstance = null;
}

/**
 * 创建新的 Pixiv 客户端实例
 */
export function createPixivClient(config?: Partial<PixivClientConfig>): PixivClient {
    return new PixivClient(config);
}
