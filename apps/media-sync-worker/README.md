# media-sync-worker

内部部署的媒体同步 worker，用于 Pixiv 图片下载任务和 Bangumi Archive 数据同步。

## 职责

- Pixiv 图片下载任务发现与消费
- Bangumi Archive 数据同步
- 通过飞书发送任务状态和失败通知

## 部署形态

本服务部署到内部 PaaS/K8s，作为 `port=0` worker 运行，不暴露 HTTP Service。

数据仍连接云主机上的 MongoDB/Redis，不使用集群内的 `mongo` / `redis` ConfigBundle。切换到内部部署前，需要停掉云主机上的旧 worker，避免重复扫描和下载。

## 必需环境变量

- `MONGO_HOST`
- `MONGO_PORT`
- `MONGO_INITDB_ROOT_USERNAME`
- `MONGO_INITDB_ROOT_PASSWORD`
- `MONGO_CONNECT_TIMEOUT_MS`
- `REDIS_HOST`
- `REDIS_PORT`
- `REDIS_PASSWORD`
- `APP_ID`
- `APP_SECRET`
- `SELF_CHAT_ID`
- `HTTP_SECRET`
- `BANGUMI_ACCESS_TOKEN`

## 部署验证开关

- `DISABLE_SCHEDULES=true`：不注册定时任务
- `DISABLE_CONSUMER=true`：不启动下载任务消费者

两个开关同时打开时，进程会保持存活，便于先验证镜像、Pod、环境变量和基础连接。

## 本地开发

```bash
bun install
bun run dev
bun run check
```

## 日志

```bash
make logs APP=media-sync-worker
```
