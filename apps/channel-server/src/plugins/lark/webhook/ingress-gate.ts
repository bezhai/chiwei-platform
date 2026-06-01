// 飞书 WS 直连入口的 env gate。
//
// HTTP webhook 已经由 channel-server 注册被动路由；WSClient 是主动长连，
// 仍需要显式打开，避免未准备好的 websocket bot 被当前进程接管。
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
