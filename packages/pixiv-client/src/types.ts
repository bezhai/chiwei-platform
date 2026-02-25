/**
 * Pixiv 通用响应结构
 */
export interface PixivGenericResponse<T> {
    error: boolean;
    message: string;
    body?: T;
}

/**
 * 关注者信息
 */
export interface FollowerInfo {
    userId: string;
    userName: string;
}

/**
 * 关注者列表响应体
 */
export interface FollowerBody {
    total: number;
    users: FollowerInfo[];
}

/**
 * 作者作品响应体
 */
export interface AuthorArtworkResponseBody {
    illusts: Record<string, any>;
}

/**
 * 插画详情
 */
export interface IllustDetail {
    id: string;
}

/**
 * 标签搜索响应体
 */
export interface TagArtworkResponseBody {
    illust: {
        data: IllustDetail[];
    };
}

/**
 * 图片 URL 集合
 */
export interface ImageUrls {
    thumb_mini: string;
    small: string;
    regular: string;
    original: string;
}

/**
 * 插画页面详情
 */
export interface IllustrationPageDetail {
    urls?: ImageUrls;
    width: number;
    height: number;
}

/**
 * 多语言标签
 */
export interface MultiTag {
    name: string;
    translation?: string;
    visible?: boolean;
}

/**
 * 飞书图片信息
 */
export interface ImageForLark {
    author?: string;
    image_key?: string;
    pixiv_addr: string;
    width?: number;
    height?: number;
    multi_tags?: MultiTag[];
    tos_file_name?: string;
}

/**
 * 通用 API 响应
 */
export interface BaseResponse<T> {
    code: number;
    msg: string;
    data: T;
}

/**
 * 分页响应
 */
export interface PaginationResponse<T> {
    total: number;
    page?: number;
    page_size?: number;
    data: T[];
}

/**
 * 图片状态模式
 */
export enum StatusMode {
    NOT_DELETE = 0,
    VISIBLE = 1,
    DELETE = 2,
    ALL = 3,
    NO_VISIBLE = 4,
}

/**
 * 获取 Pixiv 图片列表参数
 */
export interface ListPixivImageDto {
    status: StatusMode;
    page: number;
    page_size: number;
    random_mode: boolean;
    start_time?: number;
    tags?: string[];
    pixiv_addrs?: string[];
    tag_and_author?: string[];
}

/**
 * 上传图片到飞书参数
 */
export interface UploadImageToLarkDto {
    pixiv_addr: string;
}

/**
 * 上报飞书上传结果参数
 */
export interface ReportLarkUploadDto {
    pixiv_addr: string;
    image_key: string;
    width: number;
    height: number;
}

/**
 * 上传飞书响应
 */
export interface UploadLarkResp {
    image_key?: string;
    width?: number;
    height?: number;
}

/**
 * Pixiv 代理请求体
 */
export interface PixivProxyRequestBody {
    url: string;
    referer: string;
    debug?: boolean;
}

/**
 * Pixiv 客户端配置
 */
export interface PixivClientConfig {
    /** Pixiv 代理服务地址 */
    proxyHost: string;
    /** HTTP 认证密钥 */
    httpSecret: string;
    /** 默认用户 ID（用于获取关注列表） */
    defaultUserId?: string;
    /** 获取版本号的函数（用于 API 请求） */
    getVersion?: () => Promise<string | null>;
}

/**
 * 从环境变量创建默认配置
 */
export function createDefaultPixivConfig(): PixivClientConfig {
    return {
        proxyHost: process.env.PIXIV_PROXY_HOST || 'http://www.yuanzhi.xyz',
        httpSecret: process.env.HTTP_SECRET || process.env.PROXY_HTTP_SECRET || '',
        defaultUserId: '35384654',
    };
}
