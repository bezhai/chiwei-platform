import { describe, expect, it } from 'bun:test';

import { markdownToPostContent } from './post-content';

describe('markdownToPostContent', () => {
    it('纯文本变成一个 md 节点', () => {
        expect(markdownToPostContent('Hello world')).toEqual({
            content: [[{ tag: 'md', text: 'Hello world' }]],
        });
    });

    it('markdown 标记原样留给飞书自己渲染，不在这里翻译', () => {
        expect(markdownToPostContent('**bold** and *italic* text')).toEqual({
            content: [[{ tag: 'md', text: '**bold** and *italic* text' }]],
        });
    });

    it('图片把文本切成前后两段，各自一行', () => {
        expect(markdownToPostContent('Before image ![photo](img_v3_abc) after image')).toEqual({
            content: [
                [{ tag: 'md', text: 'Before image' }],
                [{ tag: 'img', image_key: 'img_v3_abc' }],
                [{ tag: 'md', text: 'after image' }],
            ],
        });
    });

    it('多张图片逐个切', () => {
        expect(markdownToPostContent('Text1 ![a](img_1) middle ![b](img_2) end')).toEqual({
            content: [
                [{ tag: 'md', text: 'Text1' }],
                [{ tag: 'img', image_key: 'img_1' }],
                [{ tag: 'md', text: 'middle' }],
                [{ tag: 'img', image_key: 'img_2' }],
                [{ tag: 'md', text: 'end' }],
            ],
        });
    });

    it('只有图片时不产出空的文字行', () => {
        expect(markdownToPostContent('![photo](img_key_123)')).toEqual({
            content: [[{ tag: 'img', image_key: 'img_key_123' }]],
        });
    });

    it('连续两张图之间不塞空行', () => {
        expect(markdownToPostContent('![a](img_1)![b](img_2)')).toEqual({
            content: [
                [{ tag: 'img', image_key: 'img_1' }],
                [{ tag: 'img', image_key: 'img_2' }],
            ],
        });
    });

    it('空输入产出一个空 md 节点（飞书不收空 content）', () => {
        expect(markdownToPostContent('')).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
    });

    it('alt 为空的图片照常成节点', () => {
        expect(markdownToPostContent('text ![](img_key) more')).toEqual({
            content: [
                [{ tag: 'md', text: 'text' }],
                [{ tag: 'img', image_key: 'img_key' }],
                [{ tag: 'md', text: 'more' }],
            ],
        });
    });

    it('跳过 https 外链图片（模型编的链接飞书显示不出来）', () => {
        expect(markdownToPostContent('before ![photo](https://example.com/pic.png) after')).toEqual({
            content: [
                [{ tag: 'md', text: 'before' }],
                [{ tag: 'md', text: 'after' }],
            ],
        });
    });

    it('跳过 http 外链图片', () => {
        expect(markdownToPostContent('![image](http://r.jina.ai/some-image.jpg)')).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
    });

    it('外链被跳光时不把原始 markdown 吐回去', () => {
        // lastIndex > 0 说明"匹配到了图片但全被跳过"。这时回退成原文等于把
        // ![...](https://...) 这串语法直接发给用户看。
        expect(
            markdownToPostContent(
                'Text ![a](img_v3_abc) middle ![b](https://files.example.com/fake.png) end',
            ),
        ).toEqual({
            content: [
                [{ tag: 'md', text: 'Text' }],
                [{ tag: 'img', image_key: 'img_v3_abc' }],
                [{ tag: 'md', text: 'middle' }],
                [{ tag: 'md', text: 'end' }],
            ],
        });
    });

    it('跳过没解析成 image_key 的注册表占位引用（N.png / @N.png）', () => {
        // 图片管线降级或没配注册表时占位引用会原样留下来。它不是飞书 image_key，
        // 当成 image_key 发出去飞书直接报错整条消息发不出。
        expect(markdownToPostContent('看图 ![pic](1.png) 和 ![pic2](@23.png)')).toEqual({
            content: [
                [{ tag: 'md', text: '看图' }],
                [{ tag: 'md', text: '和' }],
            ],
        });
    });
});
