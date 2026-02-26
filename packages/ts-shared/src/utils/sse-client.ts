/**
 * SSE client configuration options
 */
export interface SSEClientOptions {
    method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
    headers?: Record<string, string>;
    body?: unknown;
    retries?: number;
    retryDelay?: number;
    autoReconnect?: boolean;
}

/**
 * SSE message type
 */
export interface SSEMessage<T = unknown> {
    data: T;
    event?: string;
    id?: string;
    retry?: number;
}

/**
 * SSE client class
 */
export class SSEClient<T = unknown> {
    private url: string;
    private options: Required<SSEClientOptions>;
    private abortController: AbortController | null = null;
    private retryCount = 0;
    private buffer = '';
    private isConnected = false;

    constructor(url: string, options: SSEClientOptions = {}) {
        this.url = url;
        this.options = {
            method: options.method || 'GET',
            headers: {
                Accept: 'text/event-stream',
                'Cache-Control': 'no-cache',
                Connection: 'keep-alive',
                ...options.headers,
            },
            body: options.body,
            retries: options.retries ?? 3,
            retryDelay: options.retryDelay ?? 1000,
            autoReconnect: options.autoReconnect ?? true,
        };
    }

    public connect(
        onMessage: (message: SSEMessage<T>) => void,
        onError?: (error: unknown) => void,
    ): () => void {
        this.abortController = new AbortController();

        const connect = async () => {
            try {
                const response = await fetch(this.url, {
                    method: this.options.method,
                    headers: this.options.headers,
                    body: this.options.body ? JSON.stringify(this.options.body) : undefined,
                    signal: this.abortController?.signal,
                });

                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }

                if (!response.body) {
                    throw new Error('Response body is null');
                }

                this.isConnected = true;
                this.retryCount = 0;
                await this.readStream(response.body, onMessage);
            } catch (err) {
                this.isConnected = false;

                if (this.options.autoReconnect && this.retryCount < this.options.retries) {
                    this.retryCount++;
                    console.warn(
                        `SSE connection failed, retrying in ${this.options.retryDelay}ms (attempt ${this.retryCount}):`,
                        err,
                    );
                    setTimeout(() => {
                        if (!this.abortController?.signal.aborted) connect();
                    }, this.options.retryDelay);
                } else {
                    if (onError) onError(err);
                }
            }
        };

        connect();

        return () => this.close();
    }

    public close(): void {
        this.isConnected = false;
        this.abortController?.abort();
        this.abortController = null;
    }

    public getIsConnected(): boolean {
        return this.isConnected;
    }

    private async readStream(
        body: ReadableStream<Uint8Array>,
        onMessage: (message: SSEMessage<T>) => void,
    ): Promise<void> {
        const reader = body.getReader();
        const decoder = new TextDecoder();

        try {
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;

                this.buffer += decoder.decode(value, { stream: true });

                const messages = this.parseMessages();

                for (const message of messages) {
                    onMessage(message);
                }
            }
        } finally {
            reader.releaseLock();
        }
    }

    private parseMessages(): SSEMessage<T>[] {
        const messages: SSEMessage<T>[] = [];

        const messageChunks = this.buffer.split(/\r\n\r\n|\n\n/);

        this.buffer = messageChunks.pop() || '';

        for (const chunk of messageChunks) {
            if (!chunk.trim()) continue;

            const message: Partial<SSEMessage<T>> = {};
            const lines = chunk.split(/\r\n|\n/);
            const dataLines: string[] = [];

            for (const line of lines) {
                if (!line.trim()) continue;
                if (!line.includes(':')) continue;

                const colonIndex = line.indexOf(':');
                const field = line.substring(0, colonIndex).trim();
                const value = line.substring(colonIndex + 1).trim();

                switch (field) {
                    case 'data':
                        dataLines.push(value);
                        break;
                    case 'event':
                        message.event = value;
                        break;
                    case 'id':
                        message.id = value;
                        break;
                    case 'retry':
                        const retry = parseInt(value, 10);
                        if (!isNaN(retry)) message.retry = retry;
                        break;
                }
            }

            if (dataLines.length > 0) {
                const dataStr = dataLines.join('\n');
                try {
                    message.data = JSON.parse(dataStr) as T;
                } catch {
                    message.data = dataStr as unknown as T;
                }

                messages.push(message as SSEMessage<T>);
            }
        }

        return messages;
    }
}
