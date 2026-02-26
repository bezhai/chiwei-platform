// Client
export { PixivClient, getPixivClient, resetPixivClient, createPixivClient } from './client';

// Auth utilities
export { generateSalt, generateToken, sendAuthenticatedRequest, urlEncode } from './auth';

// Types - interfaces re-exported with 'type' for Bun runtime compatibility
export type {
    // Config
    PixivClientConfig,
    // Pixiv API types
    PixivGenericResponse,
    PixivProxyRequestBody,
    FollowerInfo,
    FollowerBody,
    AuthorArtworkResponseBody,
    IllustDetail,
    TagArtworkResponseBody,
    ImageUrls,
    IllustrationPageDetail,
    // Image store types
    MultiTag,
    ImageForLark,
    BaseResponse,
    PaginationResponse,
    ListPixivImageDto,
    UploadImageToLarkDto,
    ReportLarkUploadDto,
    UploadLarkResp,
} from './types';
// Runtime values (enum, function) exported separately
export { StatusMode, createDefaultPixivConfig } from './types';
