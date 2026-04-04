import { Hono } from 'hono';
import { AppDataSource, ModelMapping } from '../db';

const app = new Hono();

app.get('/api/model-mappings', async (c) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mappings = await repo.find({ order: { created_at: 'DESC' } });
  return c.json(mappings);
});

app.get('/api/model-mappings/:id', async (c) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: c.req.param('id') } });
  if (!mapping) {
    return c.json({ message: 'Not found' }, 404);
  }
  return c.json(mapping);
});

app.post('/api/model-mappings', async (c) => {
  const { alias, provider_name, real_model_name, description, model_config } =
    (await c.req.json()) as {
      alias?: string;
      provider_name?: string;
      real_model_name?: string;
      description?: string;
      model_config?: Record<string, unknown> | null;
    };

  if (!alias || !provider_name || !real_model_name) {
    return c.json({ message: 'alias, provider_name, real_model_name are required' }, 400);
  }

  const repo = AppDataSource.getRepository(ModelMapping);
  const existing = await repo.findOne({ where: { alias } });
  if (existing) {
    return c.json({ message: 'alias already exists' }, 400);
  }

  const mapping = repo.create({
    alias,
    provider_name,
    real_model_name,
    description: description ?? null,
    model_config: model_config ?? null,
  });

  await repo.save(mapping);
  return c.json(mapping);
});

app.put('/api/model-mappings/:id', async (c) => {
  const { alias, provider_name, real_model_name, description, model_config } =
    (await c.req.json()) as {
      alias?: string;
      provider_name?: string;
      real_model_name?: string;
      description?: string;
      model_config?: Record<string, unknown> | null;
    };

  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: c.req.param('id') } });
  if (!mapping) {
    return c.json({ message: 'Not found' }, 404);
  }

  if (alias !== undefined && alias !== mapping.alias) {
    const existing = await repo.findOne({ where: { alias } });
    if (existing) {
      return c.json({ message: 'alias already exists' }, 400);
    }
    mapping.alias = alias;
  }
  if (provider_name !== undefined) {
    mapping.provider_name = provider_name;
  }
  if (real_model_name !== undefined) {
    mapping.real_model_name = real_model_name;
  }
  if (description !== undefined) {
    mapping.description = description;
  }
  if (model_config !== undefined) {
    mapping.model_config = model_config;
  }

  await repo.save(mapping);
  return c.json(mapping);
});

app.delete('/api/model-mappings/:id', async (c) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: c.req.param('id') } });
  if (!mapping) {
    return c.json({ message: 'Not found' }, 404);
  }

  await repo.remove(mapping);
  return c.json({ success: true });
});

export default app;
