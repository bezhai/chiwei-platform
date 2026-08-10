import { describe, expect, it } from 'bun:test';

import { createLarkPostRenderer, type LarkRenderDeps } from './render';

interface Harness {
    deps: LarkRenderDeps;
    /** 每一步被调用的先后。顺序不变量靠它钉住。 */
    steps: string[];
    /** mention 那一步拿到的正文。它必须是**没被图片改写过**的原文。 */
    mentionSaw: string[];
    mentionChatIds: string[];
}

function harness(): Harness {
    const h: Harness = {
        deps: undefined as unknown as LarkRenderDeps,
        steps: [],
        mentionSaw: [],
        mentionChatIds: [],
    };

    h.deps = {
        async mentions(text, chatId) {
            h.steps.push('mentions');
            h.mentionSaw.push(text);
            h.mentionChatIds.push(chatId);
            return text.replaceAll('@小明', '<at user_id="on_xm">小明</at>');
        },
        images: {
            registry: {
                async lookup(registryId) {
                    h.steps.push(`lookup:${registryId}`);
                    return { '1.png': 'https://tos.example/1.png' };
                },
                async download(url) {
                    h.steps.push(`download:${url}`);
                    return Buffer.from('bytes');
                },
            },
            uploader: {
                async uploadImage() {
                    h.steps.push('upload');
                    return 'img_v3_uploaded';
                },
            },
        },
    };

    return h;
}

describe('createLarkPostRenderer', () => {
    it('纯文本：不查群成员、不查注册表，直接一个 md 节点', async () => {
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)('你好呀', {});

        expect(post).toEqual({ content: [[{ tag: 'md', text: '你好呀' }]] });
        expect(h.steps).toEqual([]);
    });

    it('群聊：@ 用给的 chat id 解析，结果落进 md 节点', async () => {
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)('喂 @小明 在吗', {
            mentionChatId: 'oc_group',
        });

        expect(h.mentionChatIds).toEqual(['oc_group']);
        expect(post).toEqual({
            content: [[{ tag: 'md', text: '喂 <at user_id="on_xm">小明</at> 在吗' }]],
        });
    });

    it('私聊（没给 chat id）：整个不解析 @，一个人都不查', async () => {
        // 私聊里没有第三个人，@ 谁都渲染不成 mention，查一次群成员纯属白花一次查询。
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)('私聊 @小明', {});

        expect(h.steps).toEqual([]);
        expect(post).toEqual({ content: [[{ tag: 'md', text: '私聊 @小明' }]] });
    });

    it('图片引用解析成飞书 image_key 之后才切节点', async () => {
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)('看图 ![我的图](1.png)', {
            imageRegistryId: 'global_msg_1',
        });

        expect(post).toEqual({
            content: [
                [{ tag: 'md', text: '看图' }],
                [{ tag: 'img', image_key: 'img_v3_uploaded' }],
            ],
        });
    });

    it('空正文也产出一个节点（飞书不收空 content）', async () => {
        const h = harness();
        expect(await createLarkPostRenderer(h.deps)('', {})).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
    });
});

describe('渲染顺序不变量：mention 必须先于图片', () => {
    // 两步都在**重写同一段文本**，所以顺序不是风格问题。mention 是按赤尾自己写下的
    // 名字去匹配的，而图片那一步会把正文里成片的区间换掉 —— 成功时换成
    // `![alt](img_v3_xxx)`，失败时换成"（图片 1.png 不可用）"这类中文。让它先跑，
    // mention 面对的就不再是赤尾写的那段话，而是一段被别的步骤加工过的文本：@ 可能
    // 凭空多出来、也可能被抹掉，两种都无声无息。
    //
    // 所以判据是**mention 必须看见原文**，而不是某个下游产物长什么样。

    const input = '@小明 看这张 ![给@小明看的图](1.png)';

    it('mention 拿到的是原文，图片引用还没被换掉', async () => {
        const h = harness();
        await createLarkPostRenderer(h.deps)(input, {
            mentionChatId: 'oc_group',
            imageRegistryId: 'g1',
        });

        expect(h.mentionSaw).toEqual([input]);
        expect(h.mentionSaw[0]).toContain('(1.png)');
        expect(h.mentionSaw[0]).not.toContain('img_v3_uploaded');
    });

    it('先 mention，后查注册表 / 下载 / 上传', async () => {
        const h = harness();
        await createLarkPostRenderer(h.deps)(input, {
            mentionChatId: 'oc_group',
            imageRegistryId: 'g1',
        });

        expect(h.steps).toEqual([
            'mentions',
            'lookup:g1',
            'download:https://tos.example/1.png',
            'upload',
        ]);
    });

    it('两步都跑完之后：@ 成了 at 标签，图成了 img 节点', async () => {
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)(input, {
            mentionChatId: 'oc_group',
            imageRegistryId: 'g1',
        });

        expect(post).toEqual({
            content: [
                [{ tag: 'md', text: '<at user_id="on_xm">小明</at> 看这张' }],
                [{ tag: 'img', image_key: 'img_v3_uploaded' }],
            ],
        });
    });
});
