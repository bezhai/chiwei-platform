import { getOss } from '@aliyun/oss';
import { resizeImage } from './image-resize';
import { uploadImage } from '@lark-client';
import { getPixivImages, reportLarkUpload } from 'infrastructure/integrations/aliyun/proxy';
import { ImageForLark, ListPixivImageDto } from 'types/pixiv';

export async function fetchUploadedImages(params: ListPixivImageDto): Promise<ImageForLark[]> {
    const images = await getPixivImages(params);

    for (const image of images) {
        if (!image.image_key) {
            if (!image.tos_file_name) {
                console.error(`Missing tos_file_name for image: ${image.pixiv_addr}`);
                continue;
            }
            const imageFile = await getOss().getFile(image.tos_file_name);
            if (!imageFile) {
                console.error(`Failed to retrieve file for OSS file name: ${image.tos_file_name}`);
                continue;
            }

            const { outFile, imgWidth, imgHeight } = await resizeImage(imageFile.content);

            const uploadRes = await uploadImage(outFile);

            // Update image object with new key and dimensions
            image.image_key = uploadRes?.image_key;
            image.width = imgWidth;
            image.height = imgHeight;

            reportLarkUpload({
                pixiv_addr: image.pixiv_addr,
                image_key: image.image_key!,
                width: imgWidth,
                height: imgHeight,
            });
        }
    }
    return images;
}
