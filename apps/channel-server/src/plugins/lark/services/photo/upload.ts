import { resizeImage } from './image-resize';
import { uploadImage } from '@lark-client';
import { ImageForLark, ListPixivImageDto } from 'types/pixiv';
import {
    getLocalPixivImageContent,
    getLocalPixivImages,
    reportLocalLarkUpload,
} from './local-source';

export async function fetchUploadedImages(params: ListPixivImageDto): Promise<ImageForLark[]> {
    const images = await getLocalPixivImages(params);

    for (const image of images) {
        if (!image.image_key) {
            try {
                if (!image.tos_file_name) {
                    console.error(`Missing tos_file_name for image: ${image.pixiv_addr}`);
                    continue;
                }

                const imageContent = await getLocalPixivImageContent(image.tos_file_name);
                if (!imageContent) {
                    console.error(`Failed to retrieve file for image: ${image.tos_file_name}`);
                    continue;
                }

                const { outFile, imgWidth, imgHeight } = await resizeImage(imageContent);

                const uploadRes = await uploadImage(outFile);
                if (!uploadRes?.image_key) {
                    console.error(`Failed to upload image to Lark: ${image.pixiv_addr}`);
                    continue;
                }

                // Update image object with new key and dimensions
                image.image_key = uploadRes.image_key;
                image.width = imgWidth;
                image.height = imgHeight;

                await reportLocalLarkUpload({
                    pixiv_addr: image.pixiv_addr,
                    image_key: image.image_key,
                    width: imgWidth,
                    height: imgHeight,
                });
            } catch (e) {
                console.error(`Failed to process image ${image.pixiv_addr}:`, e);
            }
        }
    }
    return images.filter((image) => image.image_key);
}
