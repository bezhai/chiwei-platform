import Router from '@koa/router';
import jwt from 'jsonwebtoken';

const router = new Router();
const PROXY_PREFIX = '/dashboard/api/grafana';
const AUTH_COOKIE = 'dashboard_grafana_proxy_token';

function verifyDashboardToken(token: string | undefined) {
  if (!token) {
    return false;
  }
  try {
    jwt.verify(token, process.env.DASHBOARD_JWT_SECRET!);
    return true;
  } catch {
    return false;
  }
}

function buildProxyBody(ctx: Router.RouterContext): BodyInit | undefined {
  if (ctx.method === 'GET' || ctx.method === 'HEAD') {
    return undefined;
  }

  const contentType = String(ctx.headers['content-type'] || '');
  const body = ctx.request.body;
  if (body == null) {
    return undefined;
  }

  if (contentType.includes('application/x-www-form-urlencoded') && typeof body === 'object') {
    return new URLSearchParams(
      Object.entries(body as Record<string, string>).map(([key, value]) => [key, String(value)]),
    );
  }

  if (contentType.includes('application/json') && typeof body === 'object') {
    return JSON.stringify(body);
  }

  if (typeof body === 'string') {
    return body;
  }

  return undefined;
}

function rewriteGrafanaHtml(html: string) {
  return html
    .replace('<base href="/" />', `<base href="${PROXY_PREFIX}/" />`)
    .replaceAll('"appSubUrl":""', `"appSubUrl":"${PROXY_PREFIX}"`)
    .replace(/"appUrl":"[^"]*"/g, `"appUrl":"${PROXY_PREFIX}/"`);
}

async function proxyGrafana(ctx: Router.RouterContext) {
  const grafanaHost = process.env.DASHBOARD_GRAFANA_HOST;
  if (!grafanaHost) {
    ctx.status = 503;
    ctx.body = { message: 'DASHBOARD_GRAFANA_HOST is not configured' };
    return;
  }

  const bootstrapToken = ctx.query.dashboard_token as string | undefined;
  const cookieToken = ctx.cookies.get(AUTH_COOKIE);
  const authToken = bootstrapToken || cookieToken;
  if (!verifyDashboardToken(authToken)) {
    ctx.status = 401;
    ctx.body = { message: 'Unauthorized' };
    return;
  }
  if (bootstrapToken && bootstrapToken !== cookieToken) {
    ctx.cookies.set(AUTH_COOKIE, bootstrapToken, {
      httpOnly: true,
      sameSite: 'lax',
      path: `${PROXY_PREFIX}/`,
    });
  }

  const upstreamBase = grafanaHost.endsWith('/') ? grafanaHost.slice(0, -1) : grafanaHost;
  const upstreamPath = ctx.path === PROXY_PREFIX ? '/' : ctx.path.slice(PROXY_PREFIX.length);
  const upstreamUrl = new URL(`${upstreamBase}${upstreamPath}`);
  const query = new URLSearchParams(ctx.querystring);
  query.delete('dashboard_token');
  if (query.size > 0) {
    upstreamUrl.search = query.toString();
  }

  const headers = new Headers();
  for (const [key, value] of Object.entries(ctx.headers)) {
    if (value == null) {
      continue;
    }
    const lowerKey = key.toLowerCase();
    if (['host', 'content-length', 'x-forwarded-host', 'x-forwarded-proto'].includes(lowerKey)) {
      continue;
    }
    if (Array.isArray(value)) {
      headers.set(key, value.join(', '));
    } else {
      headers.set(key, value);
    }
  }
  headers.set('host', upstreamUrl.host);

  const response = await fetch(upstreamUrl, {
    method: ctx.method,
    headers,
    body: buildProxyBody(ctx),
    redirect: 'manual',
  });

  ctx.status = response.status;

  response.headers.forEach((value, key) => {
    const lowerKey = key.toLowerCase();
    if (['content-security-policy', 'x-frame-options', 'content-length', 'transfer-encoding'].includes(lowerKey)) {
      return;
    }
    if (lowerKey === 'location') {
      const rewritten = value.startsWith('/')
        ? `${PROXY_PREFIX}${value}`
        : value.replace(upstreamBase, PROXY_PREFIX);
      ctx.set(key, rewritten);
      return;
    }
    ctx.set(key, value);
  });

  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('text/html')) {
    ctx.body = rewriteGrafanaHtml(await response.text());
    return;
  }

  const arrayBuffer = await response.arrayBuffer();
  ctx.body = Buffer.from(arrayBuffer);
}

router.all('/api/grafana', proxyGrafana);
router.all('/api/grafana/(.*)', proxyGrafana);

export default router;
