import axios, { AxiosError, AxiosInstance, AxiosRequestConfig } from 'axios';

/**
 * Header provider function type
 */
export type HeaderProvider = () => Record<string, string>;

/**
 * HTTP client options
 */
export interface HttpClientOptions {
    timeout?: number;
    headerProvider?: HeaderProvider;
    baseURL?: string;
}

/**
 * Create an HTTP client with optional header provider
 */
export function createHttpClient(options: HttpClientOptions = {}): AxiosInstance {
    const { timeout = 30000, headerProvider, baseURL } = options;

    const http = axios.create({
        timeout,
        baseURL,
    });

    http.interceptors.request.use((config) => {
        if (headerProvider) {
            const headers = headerProvider();
            for (const [key, value] of Object.entries(headers)) {
                if (value) {
                    config.headers[key] = value;
                }
            }
        }
        return config;
    });

    return http;
}

/**
 * Retry options for requests
 */
export interface RetryOptions {
    maxRetries?: number;
    retryDelay?: number;
    retryOn?: (error: AxiosError) => boolean;
}

/**
 * Request with retry functionality
 */
export async function requestWithRetry<T>(
    requestFn: () => Promise<T>,
    options: RetryOptions = {},
): Promise<T> {
    const { maxRetries = 3, retryDelay = 1000, retryOn } = options;

    const shouldRetry =
        retryOn ||
        ((error: AxiosError) => {
            // Default: retry on connection errors and 5xx errors
            if (
                error.code === 'ECONNREFUSED' ||
                error.code === 'ECONNRESET' ||
                error.code === 'ETIMEDOUT'
            ) {
                return true;
            }
            const status = error.response?.status;
            return status !== undefined && status >= 500;
        });

    let lastError: AxiosError | undefined;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
        try {
            return await requestFn();
        } catch (error) {
            lastError = error as AxiosError;

            if (attempt < maxRetries && shouldRetry(lastError)) {
                const delay = retryDelay * Math.pow(2, attempt); // Exponential backoff
                await new Promise((resolve) => setTimeout(resolve, delay));
                continue;
            }
            throw error;
        }
    }

    throw lastError;
}
