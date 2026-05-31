import { describe, it, expect } from 'bun:test';

import { larkContextStore } from './lark-context-store';
import type { Message } from '@core/models/message';

// B2 命门：lark 插件私有的 keyed context store，替代 #228 的 larkMessage 旁挂。
//
// 飞书指令需要飞书原始数据（admin 状态、群信息、原始 message_id），但 core 的
// RuleMessage 不能再携带任何飞书对象。机制：lark adapter 入站时把该消息的飞书
// Message put 进这个 store（key=全局 internalMessageId）；搬到 plugins/lark 的
// 飞书指令的谓词/handler 通过闭包 store.get(key) 拿飞书数据 —— lark→lark 插件
// 内部流转，core 永远看不到飞书对象。一次处理结束后 clear，避免内存泄漏。

// 测试只关心 store 的 key/value 行为，用最小桩冒充 Message。
function fakeLark(tag: string): Message {
    return { messageId: tag } as unknown as Message;
}

describe('LarkContextStore (plugin-private keyed store)', () => {
    it('put then get returns the same lark Message by key', () => {
        const m = fakeLark('lark-1');
        larkContextStore.put('GMID-1', m);
        expect(larkContextStore.get('GMID-1')).toBe(m);
        larkContextStore.clear('GMID-1');
    });

    it('get fail-loud when key absent (no silent skip/degrade)', () => {
        expect(() => larkContextStore.get('NOPE')).toThrow(/lark/i);
    });

    it('clear removes the entry (no memory leak across messages)', () => {
        larkContextStore.put('GMID-2', fakeLark('lark-2'));
        expect(larkContextStore.get('GMID-2')).toBeDefined();
        larkContextStore.clear('GMID-2');
        expect(() => larkContextStore.get('GMID-2')).toThrow(/lark/i);
    });

    it('distinct keys hold distinct lark Messages (concurrent messages do not collide)', () => {
        const a = fakeLark('a');
        const b = fakeLark('b');
        larkContextStore.put('KA', a);
        larkContextStore.put('KB', b);
        expect(larkContextStore.get('KA')).toBe(a);
        expect(larkContextStore.get('KB')).toBe(b);
        larkContextStore.clear('KA');
        larkContextStore.clear('KB');
    });

    it('clear of an absent key is a no-op (does not throw)', () => {
        expect(() => larkContextStore.clear('never-put')).not.toThrow();
    });
});
