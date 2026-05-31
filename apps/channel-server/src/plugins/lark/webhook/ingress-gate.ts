// 飞书直连入口（webhook + ws）的 env gate。
//
// ⚠️ 双跑红线：channel-proxy 在 ③ cutover 前仍是飞书 webhook / ws 入口。若
// channel-server 同时也连同一 bot（注册 webhook path / 起 WSClient），同一条飞书
// 消息会被两个进程各消费一次 → 双回复 / 双副作用。所以本入口默认 OFF，部署后行为
// 与现状逐字节一致；只有 ③ 在场、确认要让 channel-server 接管入口时，才把
// LARK_DIRECT_INGRESS 设为 'true' 并同步把 channel-proxy 下线。
//
// 入口是部署属性（这个进程要不要当飞书入口），不是业务行为参数，所以走环境变量
// （部署期决定）而非 dynamic config（运行时）。

export const LARK_DIRECT_INGRESS_ENV = 'LARK_DIRECT_INGRESS';

export function shouldEnableDirectIngress(envValue: string | undefined): boolean {
    return envValue === 'true';
}

export function isDirectIngressEnabled(): boolean {
    return shouldEnableDirectIngress(process.env[LARK_DIRECT_INGRESS_ENV]);
}
