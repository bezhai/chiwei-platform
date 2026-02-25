import * as lark from '@larksuiteoapi/node-sdk';
import { LarkClient } from './client';
import { LarkClientConfig } from './types';

/**
 * 多机器人客户端管理器
 * 用于管理多个 Lark 机器人客户端
 */
export class LarkClientManager {
    private static instance: LarkClientManager;
    private clientPool: Map<string, LarkClient> = new Map();
    private currentBotName: string | null = null;

    private constructor() {}

    static getInstance(): LarkClientManager {
        if (!LarkClientManager.instance) {
            LarkClientManager.instance = new LarkClientManager();
        }
        return LarkClientManager.instance;
    }

    /**
     * 批量初始化客户端
     */
    initializeClients(configs: LarkClientConfig[]): void {
        this.clientPool.clear();
        for (const config of configs) {
            if (config.botName) {
                const client = new LarkClient(config);
                this.clientPool.set(config.botName, client);
            }
        }
        console.info(`Initialized ${configs.length} Lark clients`);
    }

    /**
     * 添加或更新客户端
     */
    addOrUpdateClient(config: LarkClientConfig): void {
        if (!config.botName) {
            throw new Error('Bot name is required');
        }
        const client = new LarkClient(config);
        this.clientPool.set(config.botName, client);
    }

    /**
     * 移除客户端
     */
    removeClient(botName: string): void {
        this.clientPool.delete(botName);
    }

    /**
     * 设置当前机器人名称
     */
    setCurrentBot(botName: string): void {
        if (!this.clientPool.has(botName)) {
            throw new Error(`Lark client not found for bot: ${botName}`);
        }
        this.currentBotName = botName;
    }

    /**
     * 获取当前机器人名称
     */
    getCurrentBotName(): string | null {
        return this.currentBotName;
    }

    /**
     * 获取当前上下文的客户端
     */
    getCurrentClient(): LarkClient {
        if (!this.currentBotName) {
            throw new Error('Current bot name is not set');
        }

        const client = this.clientPool.get(this.currentBotName);
        if (!client) {
            throw new Error(`Lark client not found for bot: ${this.currentBotName}`);
        }

        return client;
    }

    /**
     * 根据机器人名称获取客户端
     */
    getClient(botName: string): LarkClient | null {
        return this.clientPool.get(botName) || null;
    }

    /**
     * 获取所有机器人名称
     */
    getAllBotNames(): string[] {
        return Array.from(this.clientPool.keys());
    }

    /**
     * 获取客户端数量
     */
    getClientCount(): number {
        return this.clientPool.size;
    }

    /**
     * 在指定机器人上下文中执行操作
     */
    async withBot<T>(botName: string, callback: (client: LarkClient) => Promise<T>): Promise<T> {
        const previousBot = this.currentBotName;
        try {
            this.setCurrentBot(botName);
            const client = this.getCurrentClient();
            return await callback(client);
        } finally {
            this.currentBotName = previousBot;
        }
    }
}

/**
 * 获取客户端管理器单例
 */
export function getLarkClientManager(): LarkClientManager {
    return LarkClientManager.getInstance();
}
