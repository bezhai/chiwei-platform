import { LoggerFactory } from '@inner/shared';
import { context } from '@middleware/context';

// Create logger with context provider that includes botName
const logger = LoggerFactory.createLogger({
    logDir: process.env.LOG_DIR || '/var/log/lark-server',
    contextProvider: () => ({
        traceId: context.getTraceId(),
        botName: context.getBotName(),
    }),
});

// Re-export LoggerFactory for advanced usage
export { LoggerFactory };

// Export default logger instance
export default logger;
