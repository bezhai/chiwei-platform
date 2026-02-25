# ARQ Worker 统一配置

## 概述

此目录包含统一的 ARQ Worker 配置

## 使用方法

### K8s 启动

生产环境由 `infra/k8s/base/workers` 中的 Deployment 负责运行，不需要单独手工启动。

### 本地开发启动

```bash
# 使用 arq 命令行工具启动
arq app.workers.unified_worker.UnifiedWorkerSettings

# 或使用 Python 模块方式
python -m arq app.workers.unified_worker.UnifiedWorkerSettings
```

## 包含的功能

### 1. 长期任务（Long Tasks）

- **定时任务**：每分钟执行一次任务轮询
- **功能**：处理异步长期任务的执行

### 2. 记忆系统（Memory System）

#### 任务函数
- `task_update_topic_memory`: 更新话题记忆

#### 定时任务
- **L2队列扫描**：每5分钟扫描一次（可配置）
- **画像刷新扫描**：每2小时扫描一次（可配置）

## 配置说明

### 环境变量

```bash
# Redis 配置
REDIS_HOST=localhost
REDIS_PASSWORD=your_password

# PostgreSQL 配置
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=your_user
POSTGRES_PASSWORD=your_password
POSTGRES_DB=your_database

# 记忆系统配置
L2_QUEUE_TRIGGER_THRESHOLD=10          # 队列触发阈值
L2_FORCE_UPDATE_AFTER_MINUTES=60       # 强制更新间隔（分钟）
L2_SCAN_INTERVAL_MINUTES=5             # 扫描间隔（分钟）
L2_QUEUE_MAX_LEN=200                   # 队列最大长度
```

## 迁移指南

### 从旧 Worker 迁移

如果你之前使用的是独立的 worker 配置：

1. **停止旧的 worker 进程**
   ```bash
   make k8s-restart SVC=arq-worker
   ```

2. **更新 K8s 清单**
   ```bash
   make k8s-apply
   ```

## 监控和日志

Worker 会输出详细的日志信息：

```python
# 查看 worker 日志
make k8s-logs SVC=arq-worker
```

关键日志信息：
- 任务执行开始/完成
- 队列处理状态
- 错误和异常信息

## 故障排查

### Worker 无法启动

1. 检查 Redis 连接：
   ```bash
   redis-cli -h $REDIS_HOST -a $REDIS_PASSWORD ping
   ```

2. 检查环境变量是否正确设置

3. 查看 worker 日志：
   ```bash
   make k8s-logs SVC=arq-worker
   ```

### 任务未执行

1. 确认 worker 正在运行：
   ```bash
   kubectl -n prod get pods -l app=arq-worker
   ```

2. 检查定时任务配置

3. 查看 Redis 中的任务队列：
   ```bash
   redis-cli -h $REDIS_HOST -a $REDIS_PASSWORD
   > KEYS arq:*
   ```

## 开发指南

### 添加新任务

1. 在相应的模块中定义任务函数
2. 在 `unified_worker.py` 中导入
3. 添加到 `functions` 列表或 `cron_jobs` 列表

```python
# unified_worker.py

from app.your_module import your_new_task

class UnifiedWorkerSettings:
    functions = [
        # 现有任务...
        your_new_task,  # 添加新任务
    ]
    
    cron_jobs = [
        # 现有定时任务...
        cron(your_cron_job, minute="*/10"),  # 每10分钟执行
    ]
```

### 测试

```bash
# 运行 worker 测试
pytest tests/test_workers/

# 手动测试任务入队
python -c "
from arq import create_pool
from app.config.config import settings
import asyncio

async def test():
    redis = await create_pool(settings.redis_settings)
    await redis.enqueue_job('task_name', arg1='value1')
    
asyncio.run(test())
"
```

## 相关文档

- [ARQ 官方文档](https://arq-docs.helpmanual.io/)
- [长期任务系统文档](../long_tasks/README.md)
- [记忆系统文档](../memory/README.md)

## 更新历史

- **2025-10-16**: 创建统一 worker 配置，整合 long_tasks 和 memory worker
