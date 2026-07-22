import { resizeImage } from './image-resize';
import { uploadImage } from '@lark-client';
import type { Readable } from 'stream';
import type { ImageForLark, ListPixivImageDto, ReportLarkUploadDto } from 'types/pixiv';
import {
    dedupePixivAddrs,
    getLocalPixivImageContent,
    getLocalPixivImageCandidates,
    reportLocalLarkUpload,
    type LocalPixivCandidateCursor,
    type LocalPixivCandidatePage,
    type LocalPixivCandidateRequest,
} from './local-source';

export interface FetchUploadedImagesDependencies {
    loadCandidates(request: LocalPixivCandidateRequest): Promise<LocalPixivCandidatePage>;
    readContent(tosFileName: string): Promise<Buffer>;
    resize(fileBuffer: Buffer): Promise<{ outFile: Readable; imgWidth: number; imgHeight: number }>;
    upload(file: Readable): Promise<{ image_key?: string } | null | undefined>;
    reportUpload(params: ReportLarkUploadDto): Promise<void>;
}

const defaultDependencies: FetchUploadedImagesDependencies = {
    loadCandidates: getLocalPixivImageCandidates,
    readContent: getLocalPixivImageContent,
    resize: resizeImage,
    upload: uploadImage,
    reportUpload: reportLocalLarkUpload,
};

export async function fetchUploadedImages(
    params: ListPixivImageDto,
    dependencies: FetchUploadedImagesDependencies = defaultDependencies,
): Promise<ImageForLark[]> {
    const target = Math.max(1, params.page_size || 6);
    const attempted = new Set<string>();
    const uploaded: ImageForLark[] = [];
    const explicitOrder = params.pixiv_addrs
        ? new Map(
              dedupePixivAddrs(params.pixiv_addrs).map((pixivAddr, index) => [pixivAddr, index]),
          )
        : null;
    let cursor: LocalPixivCandidateCursor | undefined;

    while (uploaded.length < target) {
        const previousCursorKey = candidateCursorKey(cursor);
        const previousAttemptCount = attempted.size;
        const page = await dependencies.loadCandidates({
            params,
            limit: target - uploaded.length,
            cursor,
            excludedPixivAddrs: [...attempted],
        });
        cursor = page.cursor;

        for (const image of page.images) {
            if (
                attempted.has(image.pixiv_addr) ||
                (explicitOrder && !explicitOrder.has(image.pixiv_addr))
            ) {
                continue;
            }
            attempted.add(image.pixiv_addr);
            const readyImage = await ensureUploadedImage(image, dependencies);
            if (readyImage) uploaded.push(readyImage);
            if (uploaded.length >= target) break;
        }

        if (uploaded.length >= target || page.exhausted) break;
        const cursorAdvanced = candidateCursorKey(cursor) !== previousCursorKey;
        if (attempted.size === previousAttemptCount && !cursorAdvanced) {
            console.error('Local pixiv candidate source made no progress; stopping refill');
            break;
        }
    }

    if (explicitOrder) {
        uploaded.sort(
            (left, right) =>
                (explicitOrder.get(left.pixiv_addr) ?? Number.MAX_SAFE_INTEGER) -
                (explicitOrder.get(right.pixiv_addr) ?? Number.MAX_SAFE_INTEGER),
        );
    }
    return uploaded;
}

async function ensureUploadedImage(
    image: ImageForLark,
    dependencies: FetchUploadedImagesDependencies,
): Promise<ImageForLark | null> {
    if (image.image_key) return image;

    try {
        if (!image.tos_file_name) {
            console.error(`Missing tos_file_name for image: ${image.pixiv_addr}`);
            return null;
        }

        const imageContent = await dependencies.readContent(image.tos_file_name);
        if (imageContent.length === 0) {
            console.error(`Failed to retrieve file for image: ${image.tos_file_name}`);
            return null;
        }

        const { outFile, imgWidth, imgHeight } = await dependencies.resize(imageContent);

        const uploadRes = await dependencies.upload(outFile);
        if (!uploadRes?.image_key) {
            console.error(`Failed to upload image to Lark: ${image.pixiv_addr}`);
            return null;
        }

        const readyImage = {
            ...image,
            image_key: uploadRes.image_key,
            width: imgWidth,
            height: imgHeight,
        };

        await dependencies.reportUpload({
            pixiv_addr: image.pixiv_addr,
            image_key: readyImage.image_key,
            width: imgWidth,
            height: imgHeight,
        });
        return readyImage;
    } catch (error) {
        console.error(`Failed to process image ${image.pixiv_addr}:`, error);
        return null;
    }
}

function candidateCursorKey(cursor: LocalPixivCandidateCursor | undefined): string {
    if (!cursor) return '';
    if (cursor.mode === 'explicit') return `explicit:${cursor.offset}`;
    const updateTime =
        cursor.updateTime instanceof Date
            ? cursor.updateTime.toISOString()
            : String(cursor.updateTime);
    return `ordered:${updateTime}:${String(cursor.id)}`;
}
