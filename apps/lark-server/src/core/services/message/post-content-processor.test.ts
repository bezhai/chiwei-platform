import { describe, it, expect } from 'bun:test';
import { markdownToPostContent, sanitizeFeishuMarkdown } from './post-content-processor';

describe('markdownToPostContent', () => {
    it('should convert plain text to a single md node', () => {
        const result = markdownToPostContent('Hello world');
        expect(result).toEqual({
            content: [[{ tag: 'md', text: 'Hello world' }]],
        });
    });

    it('should preserve markdown formatting in md nodes', () => {
        const md = '**bold** and *italic* text';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [[{ tag: 'md', text: '**bold** and *italic* text' }]],
        });
    });

    it('should split text and image into md + img nodes', () => {
        const md = 'Before image ![photo](img_v3_abc) after image';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [
                [{ tag: 'md', text: 'Before image' }],
                [{ tag: 'img', image_key: 'img_v3_abc' }],
                [{ tag: 'md', text: 'after image' }],
            ],
        });
    });

    it('should handle multiple images', () => {
        const md = 'Text1 ![a](img_1) middle ![b](img_2) end';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [
                [{ tag: 'md', text: 'Text1' }],
                [{ tag: 'img', image_key: 'img_1' }],
                [{ tag: 'md', text: 'middle' }],
                [{ tag: 'img', image_key: 'img_2' }],
                [{ tag: 'md', text: 'end' }],
            ],
        });
    });

    it('should handle image-only content (no surrounding text)', () => {
        const md = '![photo](img_key_123)';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [[{ tag: 'img', image_key: 'img_key_123' }]],
        });
    });

    it('should handle consecutive images with no text between', () => {
        const md = '![a](img_1)![b](img_2)';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [
                [{ tag: 'img', image_key: 'img_1' }],
                [{ tag: 'img', image_key: 'img_2' }],
            ],
        });
    });

    it('should fallback to original text when empty string', () => {
        const result = markdownToPostContent('');
        expect(result).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
    });

    it('should handle image with empty alt text', () => {
        const md = 'text ![](img_key) more';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [
                [{ tag: 'md', text: 'text' }],
                [{ tag: 'img', image_key: 'img_key' }],
                [{ tag: 'md', text: 'more' }],
            ],
        });
    });

    it('should skip external URL images (https)', () => {
        const md = 'before ![photo](https://example.com/pic.png) after';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [
                [{ tag: 'md', text: 'before' }],
                [{ tag: 'md', text: 'after' }],
            ],
        });
    });

    it('should skip external URL images (http)', () => {
        const md = '![image](http://r.jina.ai/some-image.jpg)';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
    });

    it('should keep valid image_key but skip external URLs in mixed content', () => {
        const md = 'Text ![a](img_v3_abc) middle ![b](https://files.oaiusercontent.com/fake.png) end';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [
                [{ tag: 'md', text: 'Text' }],
                [{ tag: 'img', image_key: 'img_v3_abc' }],
                [{ tag: 'md', text: 'middle' }],
                [{ tag: 'md', text: 'end' }],
            ],
        });
    });

    it('should strip bold markers around CJK book title marks 《》', () => {
        const md = '推荐**《三体》**这本书';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [[{ tag: 'md', text: '推荐《三体》这本书' }]],
        });
    });

    it('should strip bold markers around CJK brackets 【】', () => {
        const md = '**【重要】请注意**';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [[{ tag: 'md', text: '【重要】请注意' }]],
        });
    });

    it('should preserve bold markers around normal CJK text', () => {
        const md = '这是**重要通知**请查收';
        const result = markdownToPostContent(md);
        expect(result).toEqual({
            content: [[{ tag: 'md', text: '这是**重要通知**请查收' }]],
        });
    });
});

describe('sanitizeFeishuMarkdown', () => {
    it('should strip bold around text starting with 《', () => {
        expect(sanitizeFeishuMarkdown('**《三体》**')).toBe('《三体》');
    });

    it('should strip bold around text ending with 》', () => {
        expect(sanitizeFeishuMarkdown('推荐**《三体》**')).toBe('推荐《三体》');
    });

    it('should strip bold around text starting with 【', () => {
        expect(sanitizeFeishuMarkdown('**【重要】通知**')).toBe('【重要】通知');
    });

    it('should strip bold around text ending with 】', () => {
        expect(sanitizeFeishuMarkdown('**标题【注意】**')).toBe('标题【注意】');
    });

    it('should strip bold around text with 「」', () => {
        expect(sanitizeFeishuMarkdown('**「引用内容」**')).toBe('「引用内容」');
    });

    it('should strip bold around text with （）', () => {
        expect(sanitizeFeishuMarkdown('**（备注内容）**')).toBe('（备注内容）');
    });

    it('should preserve bold around normal text', () => {
        expect(sanitizeFeishuMarkdown('**重要通知**')).toBe('**重要通知**');
    });

    it('should preserve bold around English text', () => {
        expect(sanitizeFeishuMarkdown('**important**')).toBe('**important**');
    });

    it('should handle mixed bold with and without CJK punctuation', () => {
        expect(sanitizeFeishuMarkdown('推荐**《三体》**和**重要通知**'))
            .toBe('推荐《三体》和**重要通知**');
    });

    it('should not affect text without bold markers', () => {
        expect(sanitizeFeishuMarkdown('普通文本《三体》')).toBe('普通文本《三体》');
    });

    it('should strip italic around text with CJK paired punctuation', () => {
        expect(sanitizeFeishuMarkdown('*《三体》*')).toBe('《三体》');
    });

    it('should preserve italic around normal text', () => {
        expect(sanitizeFeishuMarkdown('*重要*')).toBe('*重要*');
    });

    it('should handle multiple problematic bold segments', () => {
        expect(sanitizeFeishuMarkdown('**《三体》**和**《黑暗森林》**'))
            .toBe('《三体》和《黑暗森林》');
    });

    it('should handle empty string', () => {
        expect(sanitizeFeishuMarkdown('')).toBe('');
    });

    it('should strip bold around text with Chinese double quotes ""', () => {
        expect(sanitizeFeishuMarkdown('**\u201C三体\u201D**')).toBe('\u201C三体\u201D');
    });

    it('should strip bold around text with Chinese single quotes \u2018\u2019', () => {
        expect(sanitizeFeishuMarkdown('**\u2018重要\u2019**')).toBe('\u2018重要\u2019');
    });
});
