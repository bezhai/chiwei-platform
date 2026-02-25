// 初始化日志系统
import { LoggerFactory } from './infrastructure/logger';

// 初始化日志系统
LoggerFactory.getInstance();

import {
    ApplicationManager,
    createDefaultConfig,
    setupProcessHandlers,
} from './startup/application';

(async () => {
    try {
        // 创建应用程序实例
        const config = createDefaultConfig();
        const app = new ApplicationManager(config);

        // 设置进程信号处理
        setupProcessHandlers(app);

        // 初始化并启动应用程序
        await app.initialize();
        await app.start();

        console.info('Application started successfully!');
    } catch (error) {
        console.error('Error during application startup:', error);
        process.exit(1);
    }
})();
