import { describe, it, expect } from 'bun:test';
import { markdownToPostContent } from './post-content-processor';

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
});
