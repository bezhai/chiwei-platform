import { describe, expect, it } from 'bun:test';

import { inboundImageObject } from './image-object';

// 这串是**跨服务的存储命名契约**（tool-service `image_pipeline.process_image_pipeline`
// 里的 `file_name=f"temp/{file_key}.jpg"`）。算错了不会报错，只会让每一张图都永远取不
// 到 —— 两侧代码各自看着都对。

describe('inboundImageObject — 入站图片进对象存储之后叫什么', () => {
    it('飞书的 image_key：temp/<key>.jpg', () => {
        expect(inboundImageObject('img_v3_0215d_54ab')).toBe('temp/img_v3_0215d_54ab.jpg');
    });

    it('QQ 那侧交给管线的是来源地址本身，名字照同一条规则算', () => {
        // QQ 的附件是网关原样透传的公网 url，channel-server 把它当 file_key 交给同一
        // 个管线（见 apps/channel-server 的 qq/image-pipeline.ts），所以对象名里带着
        // 整个地址。名字长得怪，但它就是那张图真正被存进去的位置。
        const url = 'https://multimedia.nt.qq.com.cn/download?fileid=abc';
        expect(inboundImageObject(url)).toBe(`temp/${url}.jpg`);
    });

    it('空 key 抛：算出来的 temp/.jpg 是一个所有空 key 共用的假位置', () => {
        expect(() => inboundImageObject('')).toThrow();
        expect(() => inboundImageObject('   ')).toThrow();
    });
});
