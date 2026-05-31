import type { Message } from '@core/models/message';

// lark 插件私有的 keyed context store —— B2 杀掉 #228「larkMessage 旁挂在
// RuleMessage 上」的逃生口后，飞书原始数据的唯一落脚点。
//
// 为什么需要它：飞书指令（admin 判定、群权限、原始 message_id、卡片回复…）
// 必须拿到飞书 Message 富对象；但 core 的 RuleMessage 是平台无关契约，绝不能
// 再携带任何飞书对象。机制是 lark→lark 的插件内部流转：
//   - lark adapter 入站派生 RuleMessage 时，把该消息的飞书 Message put 进来，
//     key = 该消息的全局 internalMessageId（跨 channel 唯一、稳定）。
//   - 搬到 plugins/lark 的飞书谓词/handler 只拿到平台无关 RuleMessage，通过
//     闭包用 message.internalMessageId 向本 store get(key) 取回飞书 Message，
//     跑不变的内部逻辑。core 永远看不到 Message —— 它不在任何 core 类型/接口上。
//   - 一次消息处理结束后 clear(key)，避免 Map 无限增长（内存泄漏）。
//
// fail-loud：get 缺 key = 装配/过滤出错（lark 指令在没 put 过的消息上跑），
// 绝不静默吞消息，在边界炸出来（与改造前 requireLarkContext fail-loud 同取向）。
class LarkContextStore {
    private readonly byMessageId = new Map<string, Message>();

    put(internalMessageId: string, larkMessage: Message): void {
        this.byMessageId.set(internalMessageId, larkMessage);
    }

    get(internalMessageId: string): Message {
        const m = this.byMessageId.get(internalMessageId);
        if (!m) {
            throw new Error(
                `lark-only rule/handler invoked but no lark Message in store for ` +
                    `message=${internalMessageId}; fail-loud — silent skip/degrade is forbidden`,
            );
        }
        return m;
    }

    clear(internalMessageId: string): void {
        this.byMessageId.delete(internalMessageId);
    }
}

// 进程级单例：lark adapter put、lark 指令 get、接线点处理结束 clear，全在
// plugins/lark 内部共享同一份。与 channel-registry / command-registry 单例同构。
export const larkContextStore = new LarkContextStore();
