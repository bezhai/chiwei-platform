import axios from 'axios';
import { sendAuthenticatedRequest, urlEncode } from './auth';
import {
    PixivClientConfig,
    createDefaultPixivConfig,
    PixivGenericResponse,
    PixivProxyRequestBody,
    BaseResponse,
    PaginationResponse,
    ImageForLark,
    ListPixivImageDto,
    UploadImageToLarkDto,
    UploadLarkResp,
    ReportLarkUploadDto,
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
    async getFollowersByTag(tag: string, userId?: string): Promise<FollowerInfo[]> {
        const targetUserId = userId || this.config.defaultUserId || '35384654';
        const pageSize = 24;
        const followers: FollowerInfo[] = [];

        let page = 1;
        let total = 0;

        do {
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
        } while (page * pageSize <= total);

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
     * 下载 Pixiv 图片内容
     */
    async downloadContent(pixivUrl: string): Promise<void> {
        const reqBody = { pixiv_url: pixivUrl };

        const response = await sendAuthenticatedRequest<BaseResponse<void>>(
            `${this.config.proxyHost}/api/v2/image-store/download`,
            reqBody,
            this.config.httpSecret,
            this.httpClient
        );

        if (response.code !== 0) {
            throw new Error(response.msg);
        }
    }

    /**
     * 获取 Pixiv 图片列表
     */
    async getPixivImages(params: ListPixivImageDto): Promise<ImageForLark[]> {
        const url = `${this.config.proxyHost}/api/v2/image-store/token-auth-list`;

        const response = await sendAuthenticatedRequest<BaseResponse<PaginationResponse<ImageForLark>>>(
            url,
            params,
            this.config.httpSecret,
            this.httpClient
        );

        if (response.code !== 0) {
            throw new Error(response.msg);
        }

        return response.data.data;
    }

    /**
     * 上传图片到飞书
     */
    async uploadToLark(params: UploadImageToLarkDto): Promise<UploadLarkResp> {
        const url = `${this.config.proxyHost}/api/v2/image-store/upload-lark`;

        const response = await sendAuthenticatedRequest<BaseResponse<UploadLarkResp>>(
            url,
            params,
            this.config.httpSecret,
            this.httpClient
        );

        if (response.code !== 0) {
            throw new Error(response.msg);
        }

        return response.data;
    }

    /**
     * 上报飞书上传结果
     */
    async reportLarkUpload(params: ReportLarkUploadDto): Promise<void> {
        const url = `${this.config.proxyHost}/api/v2/image-store/report-lark-upload`;

        const response = await sendAuthenticatedRequest<BaseResponse<void>>(
            url,
            params,
            this.config.httpSecret,
            this.httpClient
        );

        if (response.code !== 0) {
            throw new Error(response.msg);
        }
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
