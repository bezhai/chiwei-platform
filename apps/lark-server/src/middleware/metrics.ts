import { Counter, Histogram, Gauge, Registry, collectDefaultMetrics } from 'prom-client';
import type { Context, Next } from 'koa';
import Router from '@koa/router';

export const register = new Registry();
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

/**
 * Koa middleware that records Prometheus HTTP metrics.
 */
export async function metricsMiddleware(ctx: Context, next: Next): Promise<void> {
    httpRequestsInFlight.inc();
    const start = performance.now();
    try {
        await next();
    } finally {
        const duration = (performance.now() - start) / 1000;
        httpRequestsInFlight.dec();
        httpRequestsTotal.inc({ method: ctx.method, path: ctx.path, status: String(ctx.status) });
        httpRequestDuration.observe({ method: ctx.method, path: ctx.path }, duration);
    }
}

/**
 * Router with /metrics endpoint.
 */
export const metricsRouter = new Router();
metricsRouter.get('/metrics', async (ctx) => {
    ctx.set('Content-Type', register.contentType);
    ctx.body = await register.metrics();
});
