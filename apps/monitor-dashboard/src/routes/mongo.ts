import { Hono } from 'hono';
import { queryLarkEvents } from '../mongo';

const app = new Hono();

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

app.post('/api/mongo/query', async (c) => {
  const body = ((await c.req.json()) || {}) as {
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

    return c.json({
      data,
      total,
      page,
      pageSize,
    });
  } catch (error) {
    return c.json({ message: (error as Error).message }, 400);
  }
});

export default app;
