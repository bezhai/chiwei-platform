import {
    LarkCard,
    ColumnSet,
    ImgComponent,
    ButtonComponent,
    Column,
    MarkdownComponent,
    CardHeader,
} from 'feishu-card';
import { UpdatePhotoCard, FetchPhotoDetails, UpdateDailyPhotoCard } from 'types/lark';
import { StatusMode } from 'types/pixiv';
import { calcBestChunks } from './calc-photo';
import { fetchUploadedImages } from './upload';

export async function searchAndBuildPhotoCard(tags: string[], allow_send_limit_photo?: boolean) {
    let images = await fetchUploadedImages({
        status: allow_send_limit_photo ? StatusMode.NOT_DELETE : StatusMode.VISIBLE,
        page: 1,
        page_size: 6,
        random_mode: true,
        tag_and_author: tags,
    });

    if (images.length <= 0) {
        throw new Error('没有找到图片');
    }

    const { chunks, weights } = calcBestChunks(images);

    const card = new LarkCard().addElement(
        new ColumnSet()
            .setHorizontalSpacing('small')
            .addColumns(
                new Column()
                    .setWidth('weighted', weights[0])
                    .addElements(
                        ...chunks[0].map((image) =>
                            new ImgComponent(image.image_key!).setAlt(image.pixiv_addr),
                        ),
                    ),
                new Column()
                    .setWidth('weighted', weights[1])
                    .addElements(
                        ...chunks[1].map((image) =>
                            new ImgComponent(image.image_key!).setAlt(image.pixiv_addr),
                        ),
                    ),
            ),
        new ColumnSet().addColumns(
            new Column().addElements(
                new ButtonComponent().setText('换一批').addValue({
                    type: UpdatePhotoCard,
                    tags,
                }),
            ),
            new Column().addElements(
                new ButtonComponent().setText('查看详情').addValue({
                    type: FetchPhotoDetails,
                    images: images.map((image) => image.pixiv_addr),
                }),
            ),
        ),
    );

    // 因为需要支持延迟更新卡片，所以需要返回 LarkCard 的 v1 版本
    return card.toV1();
}

export async function getPhotoDetailCard(pixivAddrs: string[]) {
    let images = await fetchUploadedImages({
        status: StatusMode.ALL,
        page: 1,
        page_size: 6,
        random_mode: false,
        pixiv_addrs: pixivAddrs,
    });

    if (images.length <= 0) {
        throw new Error('没有找到图片');
    }

    const card = new LarkCard().addElement(
        ...images.map((image) => {
            const tags = image.multi_tags
                ?.filter((tag) => !!tag.translation && tag.visible)
                .map((tag) => tag.translation)
                .join('、');

            return new ColumnSet().addColumns(
                new Column()
                    .addElements(
                        new MarkdownComponent(
                            `**图片标签**：${tags}
**作者**：${image.author}
**PixivId**：${image.pixiv_addr}`,
                        ),
                    )
                    .setWidth('weighted', 1),
                new Column().addElements(
                    new ImgComponent(image.image_key!)
                        .setAlt(image.pixiv_addr)
                        .setSize('medium')
                        .setScaleType('crop_center'),
                ),
            );
        }),
    );

    return card;
}

export async function searchAndBuildDailyPhotoCard(
    start_time: number,
    allow_send_limit_photo?: boolean,
) {
    let images = await fetchUploadedImages({
        status: allow_send_limit_photo ? StatusMode.NOT_DELETE : StatusMode.VISIBLE,
        page: 1,
        page_size: 6,
        random_mode: true,
        start_time,
    });

    if (images.length <= 0) {
        throw new Error('没有找到图片');
    }

    const { chunks, weights } = calcBestChunks(images);

    const card = new LarkCard().withHeader(new CardHeader('今日新图').color('green')).addElement(
        new ColumnSet()
            .setHorizontalSpacing('small')
            .addColumns(
                new Column()
                    .setWidth('weighted', weights[0])
                    .addElements(
                        ...chunks[0].map((image) =>
                            new ImgComponent(image.image_key!).setAlt(image.pixiv_addr),
                        ),
                    ),
                new Column()
                    .setWidth('weighted', weights[1])
                    .addElements(
                        ...chunks[1].map((image) =>
                            new ImgComponent(image.image_key!).setAlt(image.pixiv_addr),
                        ),
                    ),
            ),
        new ColumnSet().addColumns(
            new Column().addElements(
                new ButtonComponent().setText('换一批').addValue({
                    type: UpdateDailyPhotoCard,
                    start_time,
                }),
            ),
            new Column().addElements(
                new ButtonComponent().setText('查看详情').addValue({
                    type: FetchPhotoDetails,
                    images: images.map((image) => image.pixiv_addr),
                }),
            ),
        ),
    );

    return card;
}
