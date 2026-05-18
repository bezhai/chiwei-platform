import { crontabRegistry } from './registry';
// 导入所有服务，触发服务注册（服务文件中会调用 registerCrontabService）
import './services';

/**
 * 初始化所有定时任务
 * 启动所有已注册的定时任务
 */
export function initializeCrontabs(): void {
    console.info('Initializing crontab services...');

    // 启动定时任务
    crontabRegistry.start();

    console.info('All crontab services initialized');
}

/**
 * 停止所有定时任务
 */
export function stopCrontabs(): void {
    crontabRegistry.stop();
}

/**
 * 获取定时任务状态
 */
export function getCrontabStatus() {
    return crontabRegistry.getStatus();
}

// 导出注册器供外部使用
export { crontabRegistry } from './registry';
