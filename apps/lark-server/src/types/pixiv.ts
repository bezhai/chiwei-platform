// Re-export types from @inner/pixiv-client
export type {
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
    ListPixivImageDto,
    UploadImageToLarkDto,
    UploadLarkResp,
    ReportLarkUploadDto,
    ImageForLark,
    MultiTag,
} from '@inner/pixiv-client';
// StatusMode is an enum (runtime value), export separately
export { StatusMode } from '@inner/pixiv-client';

// IllustData 类保留在本地
import type { IllustDetail } from '@inner/pixiv-client';

export class IllustData {
    data: IllustDetail[];

    constructor(data: IllustDetail[]) {
        this.data = data;
    }

    getIDs(): string[] {
        return this.data.map((detail) => detail.id);
    }
}
