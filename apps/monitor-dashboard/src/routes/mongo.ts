import Router from '@koa/router';
import { queryLarkEvents } from '../mongo';

const router = new Router();

const parseNumber = (value: unknown, fallback: number) => {
  if (typeof value === 'number') {
    return value;
  }
  if (typeof value === 'string') {
    const parsed = Number(value);
    return Number.isNaN(parsed) ? fallback : parsed;
  }
  return fallback;
};

router.post('/api/mongo/query', async (ctx) => {
  const body = (ctx.request.body || {}) as {
    filter?: Record<string, unknown>;
    projection?: Record<string, unknown>;
    sort?: Record<string, unknown>;
    page?: number;
    pageSize?: number;
  };

  const page = Math.max(1, parseNumber(body.page, 1));
  const pageSize = Math.min(100, Math.max(1, parseNumber(body.pageSize, 20)));

  try {
    const { data, total } = await queryLarkEvents({
      filter: body.filter ?? {},
      projection: body.projection ?? {},
      sort: body.sort ?? { created_at: -1 },
      skip: (page - 1) * pageSize,
      limit: pageSize,
    });

    ctx.body = {
      data,
      total,
      page,
      pageSize,
    };
  } catch (error) {
    ctx.status = 400;
    ctx.body = { message: (error as Error).message };
  }
});

export default router;
