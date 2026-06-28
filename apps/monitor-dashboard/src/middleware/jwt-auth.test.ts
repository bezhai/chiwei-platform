import { describe, it, expect, beforeEach } from 'bun:test';
import { Hono } from 'hono';
import { jwtAuth } from './jwt-auth';

// jwtAuth reads tokens from process.env at request time, so setting them here
// is enough — no module reload needed.
function buildApp() {
  const app = new Hono();
  app.use('/dashboard/api/*', jwtAuth as never);
  const echo = (c: { json: (b: unknown) => unknown; get: (k: never) => unknown }) =>
    c.json({ caller: c.get('caller' as never) });
  app.get('/dashboard/api/ping', echo as never);
  app.get('/dashboard/api/health', echo as never);
  return app;
}

async function call(headers: Record<string, string>, path = '/dashboard/api/ping') {
  const app = buildApp();
  const res = await app.request(path, { headers });
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON */
  }
  return { status: res.status, body };
}

describe('jwtAuth api-key', () => {
  beforeEach(() => {
    process.env.DASHBOARD_CC_TOKEN = 'CC';
    process.env.DASHBOARD_PAAS_TOKEN = 'PAAS';
    process.env.DASHBOARD_JWT_SECRET = 'secret';
  });

  it('accepts the dashboard CC token (existing callers keep working)', async () => {
    const { status, body } = await call({ 'x-api-key': 'CC' });
    expect(status).toBe(200);
    expect(body).toMatchObject({ caller: 'claude-code' });
  });

  it('accepts the paas token so one secret works for both surfaces', async () => {
    const { status, body } = await call({ 'x-api-key': 'PAAS' });
    expect(status).toBe(200);
    expect(body).toMatchObject({ caller: 'claude-code' });
  });

  it('rejects an unknown api-key with no bearer', async () => {
    const { status } = await call({ 'x-api-key': 'nope' });
    expect(status).toBe(401);
  });

  it('lets public paths through without any token', async () => {
    const { status, body } = await call({}, '/dashboard/api/health');
    expect(status).toBe(200);
    expect(body).toMatchObject({ caller: 'public' });
  });
});
