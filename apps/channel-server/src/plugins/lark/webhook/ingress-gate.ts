// 飞书 WS 直连入口的 gate：prod 部署 AND env 打开，两个条件同时满足才建长连。
//
// 泳道维度是硬约束（见 @infrastructure/lane-policy）：飞书对同一 app_id 的多个长
// 连客户端是随机投递而非广播，泳道建连会静默分走线上事件。它不给 env 后门——
// LARK_DIRECT_INGRESS 挂在 app env 上、所有泳道天然继承，只靠 env 挡不住。
//
// env 维度语义收窄为「prod 内部入站走不走长连」的回退开关：HTTP webhook 是被动
// 路由，channel-server 起来就注册；WSClient 是主动长连，仍要显式打开。
//
// 两个维度都是部署属性（这个进程要不要当飞书入口），所以读环境变量（部署期决定）
// 而不是 dynamic config（运行时）。

import { isProdDeployment } from '@infrastructure/lane-policy';

const LARK_DIRECT_INGRESS_ENV = 'LARK_DIRECT_INGRESS';

export function isDirectIngressEnabled(): boolean {
    return isProdDeployment() && process.env[LARK_DIRECT_INGRESS_ENV] === 'true';
}
