// 指令注册表（平台无关核心）。替代 engine.ts 里硬编码的 chatRules 数组。
//
// 新模型「归属 = 谁注册」：一条指令属于哪个 channel，由它在哪次 register 调用里
// 登记决定，不再靠 RuleConfig.channels 这种 flag。平台插件在自注册时把自己的
// 指令 register(channel, ...) 进来；真正平台无关的指令走 registerCore。core 不
// 知道任何具体平台指令的存在。
//
// forChannel 的顺序是契约：平台指令在前、核心通用指令在后 —— 核心指令是全渠道
// 生效的，排在后面才不会盖住某个渠道自己的同名/更窄指令。

import type { RuleConfig } from './rule';

export class CommandRegistry {
    // 平台无关的核心指令（对所有 channel 生效）。
    private core: RuleConfig[] = [];
    // channel -> 该 channel 注册的平台指令。
    private byChannel = new Map<string, RuleConfig[]>();

    // 注册平台无关核心指令。可多次调用，累加。
    registerCore(commands: RuleConfig[]): void {
        this.core.push(...commands);
    }

    // 平台插件注册自己的指令。同一 channel 可多次调用（插件分批注册），累加。
    register(channel: string, commands: RuleConfig[]): void {
        const existing = this.byChannel.get(channel);
        if (existing) {
            existing.push(...commands);
        } else {
            this.byChannel.set(channel, [...commands]);
        }
    }

    // 返回某 channel 实际生效的指令序列：先该 channel 的平台指令，后核心通用
    // 指令。返回新数组（副本），外部修改不污染注册表。
    forChannel(channel: string): RuleConfig[] {
        const channelCmds = this.byChannel.get(channel) ?? [];
        return [...channelCmds, ...this.core];
    }
}

// 进程级单例：平台插件在 import 期 register(channel, ...) 自己的指令；runRules
// 用 getCommandRegistry().forChannel(channel) 读。与 channel-registry.ts 的单例
// 取向一致——一个进程一张指令注册表，engine 与插件共享同一份。
const singleton = new CommandRegistry();

export function getCommandRegistry(): CommandRegistry {
    return singleton;
}
