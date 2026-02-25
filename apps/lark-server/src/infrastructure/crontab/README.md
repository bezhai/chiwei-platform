# Crontab 定时任务系统

基于装饰器的定时任务系统，自动扫描和注册，无需手动维护任务列表。

## 架构

- **decorators.ts**: 提供 `@Crontab` 装饰器和 `registerCrontabService` 函数
- **registry.ts**: 定时任务注册器，负责启动和管理所有定时任务
- **services/**: 存放所有定时任务服务
- **index.ts**: 统一入口，自动导入服务并启动任务

## 使用方式

### 1. 创建服务类并使用 @Crontab 装饰器

在 `services/` 目录下创建新的服务文件，例如 `my-task.ts`：

```typescript
import { Crontab, registerCrontabService } from '../decorators';

export class MyTaskService {
    /**
     * 每天 18:00 执行的任务
     */
    @Crontab('0 18 * * *', { taskName: 'my-daily-task', botName: 'bytedance' })
    async runDailyTask(): Promise<void> {
        // 任务逻辑
    }

    /**
     * 每小时执行的任务
     */
    @Crontab('0 * * * *', { taskName: 'my-hourly-sync' })
    async syncData(): Promise<void> {
        // 同步逻辑
    }
}

// 导出单例
export const myTaskService = new MyTaskService();

// 注册定时任务（重要！）
registerCrontabService(myTaskService);
```

### 2. 在 services/index.ts 中导出服务

```typescript
export * from './my-task';
```

**就这样！** 无需在其他地方手动注册，系统会自动扫描和启动任务。

### 3. 应用启动时会自动加载

在应用初始化时，`initializeCrontabs()` 会：
1. 自动导入所有服务（通过 `import './services'`）
2. 触发服务文件中的 `registerCrontabService()` 调用
3. 启动所有已注册的定时任务

## Cron 表达式格式

```
* * * * *
│ │ │ │ │
│ │ │ │ └─ 星期几 (0-7, 0 和 7 都表示星期日)
│ │ │ └─── 月份 (1-12)
│ │ └───── 日期 (1-31)
│ └─────── 小时 (0-23)
└───────── 分钟 (0-59)
```

### 常用示例

- `0 18 * * *` - 每天 18:00
- `30 19 * * *` - 每天 19:30
- `0 * * * *` - 每小时
- `*/5 * * * *` - 每 5 分钟
- `0 0 * * 0` - 每周日 00:00

## 添加新任务的步骤

1. 在 `services/` 目录下创建新的服务文件
2. 使用 `@Crontab` 装饰器标记定时任务方法
3. 创建服务单例并调用 `registerCrontabService()`
4. 在 `services/index.ts` 中导出服务

**无需修改其他任何文件！** 系统会自动发现和注册新任务。

## 特性

1. **完全自动化**: 只需添加服务文件和导出，无需手动维护注册列表
2. **装饰器化**: 直接在服务方法上声明定时任务
3. **自动上下文**: 装饰器自动为任务添加日志和上下文（botName、traceId）
4. **统一管理**: 通过 CrontabRegistry 统一管理所有定时任务
5. **易于维护**: 定时任务配置和业务逻辑在同一个文件中

## 现有任务

### Daily Photo Service
- `sendDailyPhoto()`: 每天 18:00 发送每日一图
- `dailySendNewPhoto()`: 每天 19:30 发送新图

### Emoji Service
- `syncEmojiData()`: 每小时同步一次 emoji 数据

## 工作原理

1. **类定义时**: `@Crontab` 装饰器保存方法的元数据到 WeakMap
2. **服务创建时**: 调用 `registerCrontabService(service)` 扫描服务实例，将任务注册到全局 registry
3. **应用启动时**: `initializeCrontabs()` 导入所有服务，然后调用 `crontabRegistry.start()` 启动所有任务
4. **任务执行时**: 装饰器包装的方法会自动添加日志和上下文

