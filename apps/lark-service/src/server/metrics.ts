/**
 * Prometheus 指标：进程默认指标 + HTTP 三件套（总量 / 时延 / 在途）。
 *
 * 进程级单例 registry：同一个进程里所有 Hono app 共用一份。每建一个 app 就新建
 * 一个 Registry 的话，collectDefaultMetrics 会给每份都装一套 event loop / GC
 * 采集器，句柄只增不减。
 */

import { Counter, Gauge, Histogram, Registry, collectDefaultMetrics } from 'prom-client';
import { Hono } from 'hono';
import type { Context, Next } from 'hono';

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

export async function metricsMiddleware(c: Context, next: Next): Promise<void> {
    httpRequestsInFlight.inc();
    const start = performance.now();
    try {
        await next();
    } finally {
        httpRequestsInFlight.dec();
        httpRequestsTotal.inc({
            method: c.req.method,
            path: c.req.path,
            status: String(c.res.status),
        });
        httpRequestDuration.observe(
            { method: c.req.method, path: c.req.path },
            (performance.now() - start) / 1000,
        );
    }
}

export const metricsRoutes = new Hono();
metricsRoutes.get('/metrics', async (c) =>
    c.text(await register.metrics(), 200, { 'Content-Type': register.contentType }),
);
