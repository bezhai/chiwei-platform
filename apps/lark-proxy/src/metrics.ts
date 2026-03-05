import { Counter, Histogram, Gauge, Registry, collectDefaultMetrics } from 'prom-client';
import { Hono } from 'hono';
import type { Context, Next } from 'hono';

const register = new Registry();
collectDefaultMetrics({ register });

const httpRequestsTotal = new Counter({
    name: 'http_requests_total',
    help: 'Total HTTP requests',
    labelNames: ['method', 'path', 'status'] as const,
    registers: [register],
});

const httpRequestDuration = new Histogram({
    name: 'http_request_duration_seconds',
    help: 'HTTP request duration in seconds',
    labelNames: ['method', 'path'] as const,
    registers: [register],
});

const httpRequestsInFlight = new Gauge({
    name: 'http_requests_in_flight',
    help: 'Number of HTTP requests currently being processed',
    registers: [register],
});

export async function metricsMiddleware(c: Context, next: Next): Promise<void | Response> {
    httpRequestsInFlight.inc();
    const start = performance.now();
    try {
        await next();
    } finally {
        const duration = (performance.now() - start) / 1000;
        httpRequestsInFlight.dec();
        httpRequestsTotal.inc({ method: c.req.method, path: c.req.path, status: String(c.res.status) });
        httpRequestDuration.observe({ method: c.req.method, path: c.req.path }, duration);
    }
}

export const metricsApp = new Hono();
metricsApp.get('/metrics', async (c) => {
    return c.text(await register.metrics(), 200, { 'Content-Type': register.contentType });
});
