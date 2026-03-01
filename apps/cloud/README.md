# apps/cloud/

此目录下的服务**不部署到 K8s 集群**，运行在独立的云主机上，通过 Docker 容器管理。

## 服务列表

| 服务 | 说明 | 部署方式 |
|---|---|---|
| `cronjob` | 定时任务（Pixiv 下载、Bangumi 归档） | `deploy.sh` 拉取镜像 → Docker run |

## 与 K8s 服务的区别

- 不通过 paas-engine 管理，不支持泳道路由
- 使用 Docker Compose 网络与同机的 MongoDB/Redis 通信
- 部署脚本在 `deploy.sh`，非 Makefile
