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
  ImageForLark,
  BaseResponse,
  StatusMode,
  ListPixivImageDto,
  UploadImageToLarkDto,
  UploadLarkResp,
} from "@inner/pixiv-client";

// IllustData 类保留在本地，因为它是一个工具类
import { IllustDetail } from "@inner/pixiv-client";

export class IllustData {
  data: IllustDetail[];

  constructor(data: IllustDetail[]) {
    this.data = data;
  }

  // 获取 data 中所有 IllustDetail 的 ID
  getIDs(): string[] {
    return this.data.map((detail) => detail.id);
  }
}
