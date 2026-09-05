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

    it('空输入产出一个空 md 节点（飞书不收空 content）', () => {
        expect(markdownToPostContent('')).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
    });
});

// 这一组是整条出站链的护栏：正文是一个对话模型自由生成的，它随手写出的任何图片语法
// 都会走到这里。变成 img 节点的后果不是丢一张图，是飞书**拒收整条消息** —— 她那句
// 话一个字都发不出去。所以判据只有一条：**content 里永远不出现 img 节点。**
describe('markdownToPostContent 对正文里的图片引用：一律不得变成 image_key', () => {
    it('看着像飞书 image_key 的也不算数', () => {
        // 真正的 image_key 只可能来自我们自己那次上传（见 pictures.ts）。正文里长得
        // 像 image_key 的字符串是模型编的，飞书查无此 key，整条消息被拒。
        expect(markdownToPostContent('Before image ![photo](img_v3_abc) after image')).toEqual({
            content: [
                [{ tag: 'md', text: 'Before image' }],
                [{ tag: 'md', text: 'after image' }],
            ],
        });
    });

    it('多个引用逐个丢掉，中间的文字照发', () => {
        expect(markdownToPostContent('Text1 ![a](img_1) middle ![b](img_2) end')).toEqual({
            content: [
                [{ tag: 'md', text: 'Text1' }],
                [{ tag: 'md', text: 'middle' }],
                [{ tag: 'md', text: 'end' }],
            ],
        });
    });

    it('http/https 外链也一样丢掉', () => {
        expect(markdownToPostContent('before ![photo](https://example.com/pic.png) after')).toEqual({
            content: [
                [{ tag: 'md', text: 'before' }],
                [{ tag: 'md', text: 'after' }],
            ],
        });
    });

    it('alt 为空的引用照样丢掉', () => {
        expect(markdownToPostContent('text ![](img_key) more')).toEqual({
            content: [
                [{ tag: 'md', text: 'text' }],
                [{ tag: 'md', text: 'more' }],
            ],
        });
    });

    it('相对路径、纯文件名、带 @ 的写法，全都不算数', () => {
        expect(
            markdownToPostContent('看图 ![pic](1.png) 和 ![p2](@23.png) 还有 ![p3](./a/b.jpg)'),
        ).toEqual({
            content: [
                [{ tag: 'md', text: '看图' }],
                [{ tag: 'md', text: '和' }],
                [{ tag: 'md', text: '还有' }],
            ],
        });
    });

    it('整篇只有图片语法时补一个空 md 节点，不把源码吐回去', () => {
        // lastIndex > 0 说明"匹配到了图片但全被丢掉"。这时回退成原文等于把
        // `![...](...)` 这串源码直接摆给用户看。
        expect(markdownToPostContent('![photo](img_key_123)')).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
        expect(markdownToPostContent('![a](img_1)![b](img_2)')).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
    });
});
