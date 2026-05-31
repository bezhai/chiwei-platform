// 插件注册表(core 持有)。平台插件在 import 期自注册进来;core 不认识任何
// 具体插件(它只认 ChannelPlugin 端口)。加平台 = 新增 plugins/xxx 模块 + 在
// plugins/index.ts 清单里 import 一行,本注册表与 core 主流程零改动。
//
// 设计取向:core 里不写死任何 lark/qq 工厂,改由插件 import 期自注册。

import type { ChannelPlugin } from '@core/ports/channel-plugin';

export class ChannelRegistry {
    private plugins = new Map<string, ChannelPlugin>();

    // 插件自注册。重复注册同一 channel fail-closed 抛错——禁止静默覆盖,
    // 配置/清单写重了必须在启动期炸出来,不能让后注册的悄悄顶掉前一个。
    register(plugin: ChannelPlugin): void {
        if (this.plugins.has(plugin.channel)) {
            throw new Error(
                `channel "${plugin.channel}" already registered; duplicate plugin registration`,
            );
        }
        this.plugins.set(plugin.channel, plugin);
    }

    has(channel: string): boolean {
        return this.plugins.has(channel);
    }

    // 未知 channel fail-closed:明确抛错而不是返回 null/undefined,与项目里
    // channel-registry / paas-engine ClassifyLane 一致的取向——配错 channel
    // 必须炸,绝不静默吞消息。
    get(channel: string): ChannelPlugin {
        const plugin = this.plugins.get(channel);
        if (!plugin) {
            throw new Error(
                `unknown channel "${channel}"; no plugin registered (check plugins/index.ts)`,
            );
        }
        return plugin;
    }

    channels(): string[] {
        return [...this.plugins.keys()];
    }
}

// 进程级单例:插件 import 期调 registerPlugin 自注册,core 主流程用
// getChannelRegistry 读。plugins/index.ts 是唯一 import 各插件模块的清单
// (B1 起填充),import 即触发自注册副作用。
const singleton = new ChannelRegistry();

export function registerPlugin(plugin: ChannelPlugin): void {
    singleton.register(plugin);
}

export function getChannelRegistry(): ChannelRegistry {
    return singleton;
}
