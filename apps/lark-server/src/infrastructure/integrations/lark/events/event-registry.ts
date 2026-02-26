/**
 * 事件处理器函数类型定义
 */
export type EventHandlerFunction = (params: any) => Promise<void>;

/**
 * 事件处理器注册表
 * 用于动态注册和管理事件处理器
 */
export class EventRegistry {
    private static handlers = new Map<string, EventHandlerFunction>();
    private static eventTypeMap = new Map<string, string>();

    /**
     * 注册事件处理器
     * @param eventType 事件类型
     * @param handler 处理器函数
     * @param handlerName 处理器名称（可选，用于调试）
     */
    static register(eventType: string, handler: EventHandlerFunction, handlerName?: string): void {
        const name = handlerName || handler.name || 'anonymous';
        this.handlers.set(name, handler);
        this.eventTypeMap.set(eventType, name);
        console.debug(`Registered event handler: ${eventType} -> ${name}`);
    }

    /**
     * 获取所有注册的事件处理器
     */
    static getHandlers(): Map<string, EventHandlerFunction> {
        return new Map(this.handlers);
    }

    /**
     * 获取事件类型到处理器名称的映射
     */
    static getEventTypeMap(): Map<string, string> {
        return new Map(this.eventTypeMap);
    }

    /**
     * 根据处理器名称获取处理器函数
     */
    static getHandler(handlerName: string): EventHandlerFunction | undefined {
        return this.handlers.get(handlerName);
    }

    /**
     * 根据事件类型获取处理器函数
     */
    static getHandlerByEventType(eventType: string): EventHandlerFunction | undefined {
        const handlerName = this.eventTypeMap.get(eventType);
        return handlerName ? this.handlers.get(handlerName) : undefined;
    }

    /**
     * 清空所有注册的处理器（主要用于测试）
     */
    static clear(): void {
        this.handlers.clear();
        this.eventTypeMap.clear();
    }

    /**
     * 获取所有注册的事件类型
     */
    static getRegisteredEventTypes(): string[] {
        return Array.from(this.eventTypeMap.keys());
    }

    /**
     * 检查事件类型是否已注册
     */
    static isEventTypeRegistered(eventType: string): boolean {
        return this.eventTypeMap.has(eventType);
    }
}

/**
 * 存储装饰器元数据的Map
 */
const eventHandlerMetadata = new Map<string, { eventTypes: string[]; methodName: string }>();

/**
 * 事件处理器装饰器
 * 用于标记方法为事件处理器，并存储元数据
 *
 * @param eventType 事件类型，支持单个事件或事件数组
 *
 * @example
 * // 单个事件
 * @EventHandler('im.message.receive_v1')
 * async handleMessageReceive(params: any) {
 *   // 处理逻辑
 * }
 *
 * // 多个事件
 * @EventHandler(['im.message.reaction.created_v1', 'im.message.reaction.deleted_v1'])
 * async handleReaction(params: any) {
 *   // 处理逻辑
 * }
 */
export function EventHandler(eventType: string | string[]) {
    return function (target: any, propertyKey: string, descriptor: PropertyDescriptor) {
        const handler = descriptor.value;

        if (typeof handler !== 'function') {
            throw new Error(
                `@EventHandler can only be applied to functions. Applied to: ${propertyKey}`,
            );
        }

        const eventTypes = Array.isArray(eventType) ? eventType : [eventType];

        // 存储元数据，使用类名+方法名作为key
        const className = target.constructor.name;
        const metadataKey = `${className}.${propertyKey}`;
        eventHandlerMetadata.set(metadataKey, {
            eventTypes,
            methodName: propertyKey,
        });

        return descriptor;
    };
}

/**
 * 注册事件处理器实例
 * 用于将类实例的方法注册到事件注册表
 */
export function registerEventHandlerInstance(instance: any): void {
    const className = instance.constructor.name;

    // 遍历所有存储的元数据，找到属于这个类的方法
    eventHandlerMetadata.forEach((metadata, metadataKey) => {
        if (metadataKey.startsWith(`${className}.`)) {
            const method = instance[metadata.methodName];
            if (method && typeof method === 'function') {
                // 创建绑定到实例的处理器函数
                const boundHandler: EventHandlerFunction = async (params: any) => {
                    return method.call(instance, params);
                };

                // 注册每个事件类型
                metadata.eventTypes.forEach((type) => {
                    EventRegistry.register(type, boundHandler, metadata.methodName);
                });
            }
        }
    });
}

/**
 * 获取所有事件处理器元数据（用于调试）
 */
export function getEventHandlerMetadata(): Map<
    string,
    { eventTypes: string[]; methodName: string }
> {
    return new Map(eventHandlerMetadata);
}

/**
 * 类级别的事件处理器装饰器
 * 用于标记整个类为事件处理器类，并自动扫描其中的方法
 */
export function EventHandlerClass(target: any) {
    // 获取类的所有方法
    const prototype = target.prototype;
    const methodNames = Object.getOwnPropertyNames(prototype).filter(
        (name) => name !== 'constructor' && typeof prototype[name] === 'function',
    );

    // 扫描每个方法是否有事件处理器元数据
    methodNames.forEach((methodName) => {
        const method = prototype[methodName];
        // 这里可以扩展为检查方法上的元数据来自动注册
        // 目前保持简单，只是标记类
    });

    return target;
}

/**
 * 自动扫描并注册事件处理器
 * 用于在应用启动时自动发现和注册所有事件处理器
 */
export class EventHandlerScanner {
    /**
     * 扫描指定模块并注册其中的事件处理器
     * @param modules 要扫描的模块对象数组
     */
    static scanAndRegister(modules: any[]): void {
        modules.forEach((module) => {
            Object.keys(module).forEach((key) => {
                const handler = module[key];
                if (typeof handler === 'function') {
                    // 检查函数是否有事件类型元数据
                    // 这里可以扩展为更复杂的元数据检查
                    console.debug(`Scanned potential handler: ${key}`);
                }
            });
        });
    }
}
