// 本进程的部署身份：prod 还是泳道。LANE 由 PaaS 部署时自动注入（值 = 本 release
// 的 lane），prod 部署注入 'prod' 或压根不注入，所以两者都算 prod 部署。
//
// 「是不是 prod 部署」是一批全局副作用的准入条件。这些副作用作用在共享资源上、
// 没有按泳道隔离的口径，只能 prod 独占，典型两类：
//   - 定时任务：往写死的真实会话发消息、按小时全量覆写共享表 —— 泳道跑起来就是
//     重复发消息 + 写脏 prod 数据；
//   - 渠道的长连接入站：多数 IM 平台对同一应用的多个长连客户端是**随机投递**而非
//     广播，泳道建连会静默分走一部分线上事件，prod 不断流、告警也抓不到。
// 判定写在代码里而不是再加环境变量：这类准入条件一旦挂在 env / Release 配置上，
// 配置被覆盖就会静默失效。
//
// 对位 agent-service 的 app/runtime/lane_policy.py（Python 侧同一套判断）。
//
// 注：./mq/client.ts 的 getLane() 读同一个 LANE，但答的是另一个问题——本进程的
// 队列 / 路由后缀是什么。准入判断刻意不挂在它上面：那个模块被多处测试整体
// mock.module 掉（bun 里是进程级全局），挂上去会被顺带改写。
export function isProdDeployment(laneEnv: string | undefined = process.env.LANE): boolean {
    return !laneEnv || laneEnv === 'prod';
}
