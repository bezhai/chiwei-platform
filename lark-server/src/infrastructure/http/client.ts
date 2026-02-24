import { createHttpClient } from '@inner/shared';
import { context } from '@middleware/context';

// Create HTTP client with context-aware headers
const http = createHttpClient({
    timeout: 30000,
    headerProvider: () => {
        const headers: Record<string, string> = {};
        const traceId = context.getTraceId();
        if (traceId) {
            headers['X-Trace-Id'] = traceId;
        }
        const appName = context.getBotName();
        if (appName) {
            headers['X-App-Name'] = appName;
        }
        return headers;
    },
});

export default http;
