import { Hono } from 'hono';
import { AppDataSource, SchemaMigration } from '../db';

const app = new Hono();

app.get('/api/migrations', async (c) => {
  const repo = AppDataSource.getRepository(SchemaMigration);
  const migrations = await repo.find({ order: { id: 'DESC' } });
  return c.json(migrations);
});

app.post('/api/migrations/run', async (c) => {
  const { version, name, sql, applied_by } = (await c.req.json()) as {
    version?: string;
    name?: string;
    sql?: string;
    applied_by?: string;
  };

  if (!version || !name || !sql) {
    return c.json({ message: 'version, name, sql are required' }, 400);
  }

  const repo = AppDataSource.getRepository(SchemaMigration);

  const existing = await repo.findOne({ where: { version } });
  if (existing) {
    return c.json({ message: `Migration ${version} already applied` }, 409);
  }

  const startTime = Date.now();
  let status: 'success' | 'failed' = 'success';
  let errorMessage: string | null = null;

  try {
    await AppDataSource.transaction(async (manager) => {
      await manager.query(sql);
    });
  } catch (err) {
    status = 'failed';
    errorMessage = err instanceof Error ? err.message : String(err);
  }

  const durationMs = Date.now() - startTime;

  const record = repo.create({
    version,
    name,
    sql_content: sql,
    applied_by: applied_by || 'manual',
    status,
    error_message: errorMessage,
    duration_ms: durationMs,
  });

  await repo.save(record);

  if (status === 'failed') {
    return c.json({ message: errorMessage, record }, 500);
  }

  return c.json(record);
});

export default app;
