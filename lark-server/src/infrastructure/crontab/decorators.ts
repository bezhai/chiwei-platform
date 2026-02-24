/**
 * Cron 任务的元数据（类级别）
 * 在类定义时保存装饰器信息
 */
interface CronMethodMetadata {
    methodName: string;
    cronExpression: string;
    taskName: string;
    botName: string;
}

/**
 * Cron 任务的元数据（实例级别）
 * 在注册时绑定到具体实例
 */
export interface CronTaskMetadata {
    cronExpression: string;
    taskName: string;
    botName: string;
    instance: any;
    methodName: string;
}

/**
 * 存储类级别的装饰器元数据
 */
const crontabMethodsMetadata = new WeakMap<any, CronMethodMetadata[]>();

/**
 * 存储所有 Cron 任务的注册表
 * 在服务注册时自动收集任务
 */
class CronTaskRegistry {
    private tasks: CronTaskMetadata[] = [];

    /**
     * 注册一个 Cron 任务
     */
    register(metadata: CronTaskMetadata): void {
        this.tasks.push(metadata);
    }

    /**
     * 获取所有注册的任务
     */
    getAllTasks(): CronTaskMetadata[] {
        return this.tasks;
    }

    /**
     * 清空注册表（主要用于测试）
     */
    clear(): void {
        this.tasks = [];
    }
}

/**
 * 全局任务注册表实例
 */
export const cronTaskRegistry = new CronTaskRegistry();

/**
 * 注册一个服务实例，扫描其所有 @Crontab 装饰的方法
 *
 * @param instance - 服务实例
 */
export function registerCrontabService(instance: any): void {
    const prototype = Object.getPrototypeOf(instance);
    const metadata = crontabMethodsMetadata.get(prototype);

    if (!metadata || metadata.length === 0) {
        return;
    }

    metadata.forEach((methodMeta) => {
        cronTaskRegistry.register({
            cronExpression: methodMeta.cronExpression,
            taskName: methodMeta.taskName,
            botName: methodMeta.botName,
            instance,
            methodName: methodMeta.methodName,
        });
    });
}

/**
 * Cron 任务装饰器配置
 */
export interface CrontabOptions {
    /**
     * 任务名称，用于日志
     */
    taskName: string;
    /**
     * 机器人名称，用于上下文
     */
    botName?: string;
}

/**
 * Cron 任务装饰器
 * 在方法上声明定时任务的 cron 表达式和配置
 *
 * @param cronExpression - Cron 表达式，格式：分钟 小时 日 月 周
 * @param options - 任务配置项
 *
 * @example
 * class MyService {
 *   @Crontab('0 18 * * *', { taskName: 'daily-task', botName: 'bytedance' })
 *   async runDailyTask() {
 *   }
 * }
 */
export function Crontab(cronExpression: string, options: CrontabOptions) {
    return function (target: any, propertyKey: string, descriptor: PropertyDescriptor): PropertyDescriptor {
        const { taskName, botName = 'bytedance' } = options;

        // 保存类级别的元数据
        const existingMetadata = crontabMethodsMetadata.get(target) || [];
        existingMetadata.push({
            methodName: propertyKey,
            cronExpression,
            taskName,
            botName,
        });
        crontabMethodsMetadata.set(target, existingMetadata);

        return descriptor;
    };
}
