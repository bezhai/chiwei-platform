import winston from 'winston';
import path from 'node:path';
import util from 'node:util';

function errorToPlainObject(error: Error): Record<string, unknown> {
    const plain: Record<string, unknown> = {
        name: error.name,
        message: error.message,
        stack: error.stack,
    };

    // Node.js v16+ / modern TS: Error may have `cause`
    const cause = (error as unknown as { cause?: unknown }).cause;
    if (cause !== undefined) {
        plain.cause = cause;
    }

    // Preserve enumerable custom fields on Error subclasses
    for (const [key, value] of Object.entries(error)) {
        if (!(key in plain)) {
            plain[key] = value;
        }
    }

    return plain;
}

function safeJsonStringify(value: unknown): string {
    const seen = new WeakSet<object>();
    return JSON.stringify(
        value,
        (_key, val: unknown) => {
            if (val instanceof Error) {
                return errorToPlainObject(val);
            }

            if (typeof val === 'bigint') {
                return val.toString();
            }

            if (typeof val === 'object' && val !== null) {
                const obj = val as object;
                if (seen.has(obj)) {
                    return '[Circular]';
                }
                seen.add(obj);
            }

            return val;
        },
    );
}

function formatConsoleArg(arg: unknown): string {
    if (arg instanceof Error) {
        return arg.stack || `${arg.name}: ${arg.message}`;
    }
    if (typeof arg === 'string') {
        return arg;
    }
    if (arg === null) {
        return 'null';
    }
    if (arg === undefined) {
        return 'undefined';
    }
    if (typeof arg === 'bigint') {
        return arg.toString();
    }
    if (typeof arg === 'object') {
        return util.inspect(arg, { depth: 6, breakLength: 120 });
    }
    return String(arg);
}

/**
 * Context provider interface for getting trace context
 */
export interface ContextProvider {
    (): { traceId?: string; [key: string]: unknown };
}

/**
 * Logger configuration interface
 */
export interface LoggerConfig {
    level: string;
    enableFileLogging: boolean;
    logDir: string;
    logFileName: string;
    maxFileSize: number;
    maxFiles: number;
    enableConsoleOverride: boolean;
    /**
     * Optional context provider for getting trace ID and other context
     */
    contextProvider?: ContextProvider;
}

/**
 * Logger transport factory
 */
export class LoggerTransportFactory {
    /**
     * Create console transport
     */
    static createConsoleTransport(contextProvider?: ContextProvider): winston.transport {
        return new winston.transports.Console({
            format: winston.format.combine(
                winston.format.errors({ stack: true }),
                winston.format.colorize(),
                winston.format.printf((info) => {
                    const traceId = contextProvider?.()?.traceId || '';
                    const message = info.stack || info.message;
                    return `${info.level}: ${message} ${traceId ? `[traceId: ${traceId}]` : ''}`;
                }),
            ),
        });
    }

    /**
     * Create file transport
     */
    static createFileTransport(config: LoggerConfig): winston.transport {
        return new winston.transports.File({
            filename: path.join(config.logDir, config.logFileName),
            maxsize: config.maxFileSize,
            maxFiles: config.maxFiles,
            format: winston.format.combine(
                winston.format.errors({ stack: true }),
                winston.format.timestamp(),
                winston.format.printf((info) => {
                    const traceId = config.contextProvider?.()?.traceId || '';
                    return safeJsonStringify({
                        ...info,
                        traceId,
                    });
                }),
            ),
        });
    }

    /**
     * Create custom transport (for testing)
     */
    static createCustomTransport(transport: winston.transport): winston.transport {
        return transport;
    }
}

/**
 * Logger factory class
 */
export class LoggerFactory {
    private static instance: winston.Logger | null = null;
    private static config: LoggerConfig | null = null;

    /**
     * Create default configuration
     */
    static createDefaultConfig(contextProvider?: ContextProvider): LoggerConfig {
        return {
            level: process.env.LOG_LEVEL || 'info',
            enableFileLogging: process.env.ENABLE_FILE_LOGGING === 'true',
            logDir: process.env.LOG_DIR || '/var/log/app',
            logFileName: 'app.log',
            maxFileSize: 5242880, // 5MB
            maxFiles: 5,
            enableConsoleOverride: true,
            contextProvider,
        };
    }

    /**
     * Create logger instance
     */
    static createLogger(config?: Partial<LoggerConfig>): winston.Logger {
        const fullConfig = { ...this.createDefaultConfig(), ...config };
        this.config = fullConfig;

        const transports: winston.transport[] = [
            LoggerTransportFactory.createConsoleTransport(fullConfig.contextProvider),
        ];

        // Add file transport if enabled
        if (fullConfig.enableFileLogging) {
            transports.push(LoggerTransportFactory.createFileTransport(fullConfig));
        }

        const logger = winston.createLogger({
            level: fullConfig.level,
            transports,
        });

        // Override console methods if enabled and file logging is on
        if (fullConfig.enableConsoleOverride && fullConfig.enableFileLogging) {
            this.overrideConsoleMethods(logger, fullConfig.contextProvider);
        }

        this.instance = logger;
        return logger;
    }

    /**
     * Create test logger (with custom transports)
     */
    static createTestLogger(transports: winston.transport[]): winston.Logger {
        return winston.createLogger({
            level: 'debug',
            transports,
        });
    }

    /**
     * Get logger instance
     */
    static getInstance(): winston.Logger {
        if (!this.instance) {
            return this.createLogger();
        }
        return this.instance;
    }

    /**
     * Reset logger instance (for testing)
     */
    static reset(): void {
        this.instance = null;
        this.config = null;
    }

    /**
     * Override console methods
     */
    private static overrideConsoleMethods(
        logger: winston.Logger,
        contextProvider?: ContextProvider
    ): void {
        // eslint-disable-next-line no-console
        console.log = (...args) => {
            const traceId = contextProvider?.()?.traceId || '';
            logger.info(args.map(formatConsoleArg).join(' '), { traceId });
        };

        console.error = (...args) => {
            const traceId = contextProvider?.()?.traceId || '';
            logger.error(args.map(formatConsoleArg).join(' '), { traceId });
        };

        console.info = (...args) => {
            const traceId = contextProvider?.()?.traceId || '';
            logger.info(args.map(formatConsoleArg).join(' '), { traceId });
        };

        console.warn = (...args) => {
            const traceId = contextProvider?.()?.traceId || '';
            logger.warn(args.map(formatConsoleArg).join(' '), { traceId });
        };
    }

    /**
     * Get current configuration
     */
    static getConfig(): LoggerConfig | null {
        return this.config;
    }
}
