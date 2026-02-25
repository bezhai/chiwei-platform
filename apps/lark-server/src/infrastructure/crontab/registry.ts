import * as cron from 'node-cron';
import { cronTaskRegistry, CronTaskMetadata } from './decorators';
import { context } from 'middleware/context';

/**
 * Cron 任务注册器
 * 负责启动和管理所有使用 @Crontab 装饰器声明的定时任务
 */
export class CrontabRegistry {
    private scheduledTasks: Map<string, cron.ScheduledTask> = new Map();

    /**
     * 启动所有已注册的定时任务
     */
    start(): void {
        console.info('Starting all cron tasks...');

        const tasks = cronTaskRegistry.getAllTasks();

        tasks.forEach((metadata: CronTaskMetadata) => {
            this.scheduleTask(metadata);
        });

        console.info(`Total ${this.scheduledTasks.size} cron task(s) started`);
    }

    /**
     * 调度单个任务
     *
     * @param metadata - 任务元数据
     */
    private scheduleTask(metadata: CronTaskMetadata): void {
        const { cronExpression, taskName, botName, methodName, instance } = metadata;
        const taskKey = `${instance.constructor.name}.${methodName}`;

        console.info(
            `Scheduling cron task: [${taskName}] ${taskKey} with expression: ${cronExpression}`,
        );

        // 创建定时任务，包装原方法添加日志和上下文
        const task = cron.schedule(cronExpression, async () => {
            console.info(`[${taskName}] Starting scheduled task...`);

            // 创建包含 botName 和 traceId 的上下文
            const contextData = context.createContext(botName);

            try {
                // 在 AsyncLocalStorage 上下文中执行处理函数
                const result = await context.run(contextData, async () => {
                    return await instance[methodName]();
                });

                console.info(`[${taskName}] Task completed successfully`);
                return result;
            } catch (error) {
                console.error(`[${taskName}] Task failed:`, error);
            }
        });

        // 启动任务
        task.start();

        // 保存任务引用
        this.scheduledTasks.set(taskKey, task);

        console.info(`Cron task started: [${taskName}] ${taskKey}`);
    }

    /**
     * 停止所有定时任务
     */
    stop(): void {
        console.info('Stopping all cron tasks...');

        this.scheduledTasks.forEach((task, taskKey) => {
            task.stop();
            console.info(`Stopped cron task: ${taskKey}`);
        });

        this.scheduledTasks.clear();
        console.info('All cron tasks stopped');
    }

    /**
     * 获取定时任务状态
     */
    getStatus(): {
        totalTasks: number;
        tasks: string[];
    } {
        return {
            totalTasks: this.scheduledTasks.size,
            tasks: Array.from(this.scheduledTasks.keys()),
        };
    }
}

/**
 * 全局 Cron 注册器实例
 */
export const crontabRegistry = new CrontabRegistry();
