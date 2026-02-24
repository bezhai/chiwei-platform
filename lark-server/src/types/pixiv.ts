// Re-export types from @inner/pixiv-client
export {
    PixivGenericResponse,
    FollowerInfo,
    FollowerBody,
    AuthorArtworkResponseBody,
    IllustDetail,
    TagArtworkResponseBody,
    ImageUrls,
    IllustrationPageDetail,
    PaginationResponse,
    BaseResponse,
    StatusMode,
    ListPixivImageDto,
    UploadImageToLarkDto,
    UploadLarkResp,
    ReportLarkUploadDto,
    // 直接使用 @inner/pixiv-client 的 ImageForLark
    ImageForLark,
    MultiTag,
} from '@inner/pixiv-client';

// IllustData 类保留在本地
import { IllustDetail } from '@inner/pixiv-client';

export class IllustData {
    data: IllustDetail[];

    constructor(data: IllustDetail[]) {
        this.data = data;
    }

    getIDs(): string[] {
        return this.data.map((detail) => detail.id);
    }
}
