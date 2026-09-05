import { describe, expect, it } from 'bun:test';

import { createLarkPostRenderer, type LarkRenderDeps } from './render';

interface Harness {
    deps: LarkRenderDeps;
    /** 每一步被调用的先后。 */
    steps: string[];
    /** mention 那一步拿到的正文。它必须是**赤尾原话**，没被任何别的步骤加工过。 */
    mentionSaw: string[];
    mentionChatIds: string[];
}

function harness(
    over: { sign?: (fileName: string) => Promise<string | null> } = {},
): Harness {
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
        pictures: {
            async sign(fileName) {
                h.steps.push(`sign:${fileName}`);
                return over.sign ? await over.sign(fileName) : `https://tos.example/${fileName}`;
            },
            async download(url) {
                h.steps.push(`download:${url}`);
                return Buffer.from('bytes');
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
    it('纯文本：不查群成员、不签图，直接一个 md 节点', async () => {
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

    it('空正文也产出一个节点（飞书不收空 content）', async () => {
        const h = harness();
        expect(await createLarkPostRenderer(h.deps)('', {})).toEqual({
            content: [[{ tag: 'md', text: '' }]],
        });
    });
});

describe('结构化图片：句柄现签之后接在正文后面', () => {
    it('一个句柄 → 正文一行、图一行', async () => {
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)('看这张', {
            pictureFileNames: ['pictures/cat.png'],
        });

        expect(post).toEqual({
            content: [
                [{ tag: 'md', text: '看这张' }],
                [{ tag: 'img', image_key: 'img_v3_uploaded' }],
            ],
        });
        expect(h.steps).toEqual([
            'sign:pictures/cat.png',
            'download:https://tos.example/pictures/cat.png',
            'upload',
        ]);
    });

    it('图挂了只降级成一行文字，正文照发', async () => {
        const h = harness({ sign: async () => null });
        const post = await createLarkPostRenderer(h.deps)('看这张', {
            pictureFileNames: ['pictures/cat.png'],
        });

        expect(post.content[0]).toEqual([{ tag: 'md', text: '看这张' }]);
        expect(post.content).toHaveLength(2);
        expect(post.content[1]![0]!.tag).toBe('md');
    });

    it('没有 pictureFileNames 的老消息：一次都不签，逐字就是没有图的老样子', async () => {
        // DLQ 里躺着的、旧版 agent-service 发的消息就是这种。
        const h = harness();
        const withoutField = await createLarkPostRenderer(h.deps)('在的', {});
        const withEmpty = await createLarkPostRenderer(h.deps)('在的', { pictureFileNames: [] });

        expect(withoutField).toEqual({ content: [[{ tag: 'md', text: '在的' }]] });
        expect(withEmpty).toEqual(withoutField);
        expect(h.steps).toEqual([]);
    });
});

describe('护栏：正文里的图片引用毒不倒整条消息', () => {
    // 她说话是两步：send_message(what) 收「意思」，然后 voice 模型自由渲染成人话。
    // voice 随手写出的任何一个 markdown 图片引用都会走到这里，而飞书认不出那个
    // image_key 就**拒收整条消息** —— 不是丢一张图，是她那句话整条发不出去。
    //
    // 判据：正文里的引用一个都不变成 img 节点，而结构化那张图正常发出。

    it('结构化图片有效、同时正文里带一个非法引用：图照发，正文那个引用没变成 image_key', async () => {
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)(
            '先看这个 ![我编的](img_v3_totally_made_up) 再看那个',
            { mentionChatId: 'oc_group', pictureFileNames: ['pictures/real.png'] },
        );

        expect(post).toEqual({
            content: [
                [{ tag: 'md', text: '先看这个' }],
                [{ tag: 'md', text: '再看那个' }],
                [{ tag: 'img', image_key: 'img_v3_uploaded' }],
            ],
        });

        // 整条消息里唯一的 img 节点，key 来自我们自己那次上传。
        const imgKeys = post.content
            .flat()
            .filter((node) => node.tag === 'img')
            .map((node) => (node as { image_key: string }).image_key);
        expect(imgKeys).toEqual(['img_v3_uploaded']);
        expect(imgKeys).not.toContain('img_v3_totally_made_up');
    });

    it('正文里几种写法（外链 / 文件名 / 相对路径）全都变不出 img 节点', async () => {
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)(
            '![a](https://x.example/p.png) ![b](1.png) ![c](./d/e.jpg)',
            {},
        );

        expect(post.content.flat().some((node) => node.tag === 'img')).toBe(false);
        expect(h.steps).toEqual([]);
    });
});

describe('渲染顺序不变量：mention 看见的必须是赤尾原话', () => {
    // mention 匹配的是**赤尾自己写下的名字**。让别的步骤先改写正文，mention 面对的
    // 就不再是她写的那段话：@ 可能凭空多出来、也可能被抹掉，两种都无声无息 ——
    // 发出去的消息看着通顺，只是 @ 错了人或者少了一个。
    //
    // 图片那一步现在**根本不碰正文**（它只往后面追加行），所以这条不变量只剩一句：
    // mention 跑在切节点之前，拿到的是原文。

    const input = '@小明 看这张 ![给@小明看的图](cat.png)';

    it('mention 拿到原文，图片语法还在里面', async () => {
        const h = harness();
        await createLarkPostRenderer(h.deps)(input, {
            mentionChatId: 'oc_group',
            pictureFileNames: ['pictures/cat.png'],
        });

        expect(h.mentionSaw).toEqual([input]);
    });

    it('先 mention，后现签 / 下载 / 上传', async () => {
        const h = harness();
        await createLarkPostRenderer(h.deps)(input, {
            mentionChatId: 'oc_group',
            pictureFileNames: ['pictures/cat.png'],
        });

        expect(h.steps).toEqual([
            'mentions',
            'sign:pictures/cat.png',
            'download:https://tos.example/pictures/cat.png',
            'upload',
        ]);
    });

    it('两步都跑完之后：@ 成了 at 标签，正文那个引用被丢掉，结构化的图成了 img 节点', async () => {
        const h = harness();
        const post = await createLarkPostRenderer(h.deps)(input, {
            mentionChatId: 'oc_group',
            pictureFileNames: ['pictures/cat.png'],
        });

        expect(post).toEqual({
            content: [
                [{ tag: 'md', text: '<at user_id="on_xm">小明</at> 看这张' }],
                [{ tag: 'img', image_key: 'img_v3_uploaded' }],
            ],
        });
    });
});
