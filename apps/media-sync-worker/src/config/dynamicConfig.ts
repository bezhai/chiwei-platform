import { DynamicConfig } from "@inner/shared";

// worker 进程内唯一的 Dynamic Config 客户端：SDK 按实例缓存 10s 快照，共用一个实例
// 才不会每类配置各拉一次。cron 和 consumer 没有请求上下文，读 prod 泳道。
export const dynamicConfig = new DynamicConfig();

/** 读取 Dynamic Config 只需要 get；测试注入同签名的替身。 */
export type DynamicConfigReader = Pick<DynamicConfig, "get">;
