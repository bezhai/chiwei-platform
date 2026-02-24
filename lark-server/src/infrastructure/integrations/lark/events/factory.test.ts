// Mock heavy dependencies that factory.ts imports but tests don't need
jest.mock('core/models/message', () => ({
    Message: { fromEvent: jest.fn() },
}));
jest.mock('@lark/utils/mention-utils', () => ({
    MentionUtils: {
        addMentions: jest.fn(() => []),
        addMentionMap: jest.fn(() => ({})),
    },
}));

import { ContentType } from '@core/models/message-content';
import { MessageTransferer } from './factory';

describe('MessageTransferer.getContentFactory', () => {
    describe('TextMessageContentFactory', () => {
        it('should parse valid text content', () => {
            const factory = MessageTransferer.getContentFactory(
                'text',
                JSON.stringify({ text: 'hello world' }),
            );
            const items = factory.generateContent();
            expect(items).toEqual([{ type: ContentType.Text, value: 'hello world' }]);
        });

        it('should return placeholder on invalid JSON', () => {
            const factory = MessageTransferer.getContentFactory('text', 'not json');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].type).toBe(ContentType.Text);
            expect(items[0].value).toBe('[文本]');
        });
    });

    describe('ImageMessageContentFactory', () => {
        it('should parse valid image content', () => {
            const factory = MessageTransferer.getContentFactory(
                'image',
                JSON.stringify({ image_key: 'img_abc123' }),
            );
            const items = factory.generateContent();
            expect(items).toEqual([{ type: ContentType.Image, value: 'img_abc123' }]);
        });

        it('should return placeholder on invalid JSON', () => {
            const factory = MessageTransferer.getContentFactory('image', '{bad}');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].value).toBe('[图片]');
        });
    });

    describe('StickerMessageContentFactory', () => {
        it('should parse valid sticker content', () => {
            const factory = MessageTransferer.getContentFactory(
                'sticker',
                JSON.stringify({ file_key: 'sticker_abc' }),
            );
            const items = factory.generateContent();
            expect(items).toEqual([{ type: ContentType.Sticker, value: 'sticker_abc' }]);
        });

        it('should return placeholder on invalid JSON', () => {
            const factory = MessageTransferer.getContentFactory('sticker', '');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].value).toBe('[表情包]');
        });
    });

    describe('PostMessageContentFactory', () => {
        it('should parse valid post content with text and image nodes', () => {
            const postContent = {
                content: [
                    [
                        { tag: 'text', text: 'hello ' },
                        { tag: 'img', image_key: 'img_post_123' },
                    ],
                    [{ tag: 'text', text: 'second line' }],
                ],
            };
            const factory = MessageTransferer.getContentFactory(
                'post',
                JSON.stringify(postContent),
            );
            const items = factory.generateContent();
            expect(items).toHaveLength(3);
            expect(items[0]).toEqual({ type: ContentType.Text, value: 'hello ' });
            expect(items[1]).toEqual({ type: ContentType.Image, value: 'img_post_123' });
            expect(items[2]).toEqual({ type: ContentType.Text, value: 'second line' });
        });

        it('should return placeholder for empty post content', () => {
            const factory = MessageTransferer.getContentFactory(
                'post',
                JSON.stringify({ content: [[]] }),
            );
            const items = factory.generateContent();
            expect(items).toEqual([{ type: ContentType.Text, value: '[富文本]' }]);
        });

        it('should return placeholder on invalid JSON', () => {
            const factory = MessageTransferer.getContentFactory('post', 'invalid');
            const items = factory.generateContent();
            expect(items).toEqual([{ type: ContentType.Text, value: '[富文本]' }]);
        });
    });

    describe('MediaMessageContentFactory', () => {
        it('should parse valid media content', () => {
            const mediaContent = {
                file_key: 'file_media_123',
                image_key: 'img_thumb_123',
                file_name: 'video.mp4',
                duration: 30,
            };
            const factory = MessageTransferer.getContentFactory(
                'media',
                JSON.stringify(mediaContent),
            );
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].type).toBe(ContentType.Media);
            expect(items[0].value).toBe('file_media_123');
            expect(items[0].meta).toEqual({
                image_key: 'img_thumb_123',
                file_name: 'video.mp4',
                duration: 30,
            });
        });

        it('should return placeholder on invalid JSON', () => {
            const factory = MessageTransferer.getContentFactory('media', 'bad');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].value).toBe('[视频]');
        });
    });

    describe('FileMessageContentFactory', () => {
        it('should parse valid file content', () => {
            const fileContent = { file_key: 'file_abc', file_name: 'report.pdf' };
            const factory = MessageTransferer.getContentFactory(
                'file',
                JSON.stringify(fileContent),
            );
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].type).toBe(ContentType.File);
            expect(items[0].value).toBe('file_abc');
            expect(items[0].meta).toEqual({ file_name: 'report.pdf' });
        });

        it('should return placeholder on invalid JSON', () => {
            const factory = MessageTransferer.getContentFactory('file', '');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].value).toBe('[文件]');
        });
    });

    describe('AudioMessageContentFactory', () => {
        it('should parse valid audio content', () => {
            const audioContent = { file_key: 'audio_abc', duration: 10 };
            const factory = MessageTransferer.getContentFactory(
                'audio',
                JSON.stringify(audioContent),
            );
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].type).toBe(ContentType.Audio);
            expect(items[0].value).toBe('audio_abc');
            expect(items[0].meta).toEqual({ duration: 10 });
        });

        it('should return placeholder on invalid JSON', () => {
            const factory = MessageTransferer.getContentFactory('audio', 'bad');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].value).toBe('[语音]');
        });
    });

    describe('MergeForwardMessageContentFactory', () => {
        it('should return unsupported item', () => {
            const factory = MessageTransferer.getContentFactory('merge_forward', '{}');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].type).toBe(ContentType.Unsupported);
            expect(items[0].value).toBe('[合并转发]');
            expect(items[0].meta).toEqual({ original_type: 'merge_forward' });
        });
    });

    describe('ShareChatMessageContentFactory', () => {
        it('should return unsupported item', () => {
            const factory = MessageTransferer.getContentFactory('share_chat', '{}');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].type).toBe(ContentType.Unsupported);
            expect(items[0].value).toBe('[分享群名片]');
        });
    });

    describe('ShareUserMessageContentFactory', () => {
        it('should return unsupported item', () => {
            const factory = MessageTransferer.getContentFactory('share_user', '{}');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].type).toBe(ContentType.Unsupported);
            expect(items[0].value).toBe('[分享个人名片]');
        });
    });

    describe('UnsupportedMessageContentFactory (unknown type)', () => {
        it('should return unsupported item with original type', () => {
            const factory = MessageTransferer.getContentFactory('todo', '{}');
            const items = factory.generateContent();
            expect(items).toHaveLength(1);
            expect(items[0].type).toBe(ContentType.Unsupported);
            expect(items[0].value).toBe('[todo]');
            expect(items[0].meta).toEqual({ original_type: 'todo' });
        });

        it('should never return empty array', () => {
            const factory = MessageTransferer.getContentFactory('completely_unknown_type', '');
            const items = factory.generateContent();
            expect(items.length).toBeGreaterThan(0);
        });
    });
});
