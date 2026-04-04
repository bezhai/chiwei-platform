import { Hono } from 'hono';
import { AppDataSource, ModelProvider } from '../db';
import { randomUUID } from 'crypto';

const app = new Hono();

const maskApiKey = (apiKey: string) => {
  if (!apiKey) {
    return '';
  }
  const tail = apiKey.slice(-4);
  return `****${tail}`;
};

const allowedClientTypes = new Set(['openai', 'openai-responses', 'deepseek', 'ark', 'azure-http', 'google']);

app.get('/api/providers', async (c) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const providers = await repo.find({ order: { created_at: 'DESC' } });
  return c.json(providers.map((provider) => ({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  })));
});

app.get('/api/providers/:id', async (c) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: c.req.param('id') } });
  if (!provider) {
    return c.json({ message: 'Not found' }, 404);
  }
  return c.json({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  });
});

app.post('/api/providers', async (c) => {
  const { name, api_key, base_url, client_type, is_active } = (await c.req.json()) as {
    name?: string;
    api_key?: string;
    base_url?: string;
    client_type?: string;
    is_active?: boolean;
  };

  if (!name || !base_url || !api_key) {
    return c.json({ message: 'name, base_url, api_key are required' }, 400);
  }

  const resolvedClientType = (client_type || 'openai').toLowerCase();
  if (!allowedClientTypes.has(resolvedClientType)) {
    return c.json({ message: 'Invalid client_type' }, 400);
  }

  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = repo.create({
    provider_id: randomUUID(),
    name,
    api_key,
    base_url,
    client_type: resolvedClientType,
    is_active: is_active ?? true,
  });

  await repo.save(provider);

  return c.json({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  });
});

app.put('/api/providers/:id', async (c) => {
  const { name, api_key, base_url, client_type, is_active } = (await c.req.json()) as {
    name?: string;
    api_key?: string;
    base_url?: string;
    client_type?: string;
    is_active?: boolean;
  };

  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: c.req.param('id') } });
  if (!provider) {
    return c.json({ message: 'Not found' }, 404);
  }

  if (name !== undefined) {
    provider.name = name;
  }
  if (api_key !== undefined && api_key !== '') {
    provider.api_key = api_key;
  }
  if (base_url !== undefined) {
    provider.base_url = base_url;
  }
  if (client_type !== undefined) {
    const resolvedClientType = client_type.toLowerCase();
    if (!allowedClientTypes.has(resolvedClientType)) {
      return c.json({ message: 'Invalid client_type' }, 400);
    }
    provider.client_type = resolvedClientType;
  }
  if (is_active !== undefined) {
    provider.is_active = is_active;
  }

  await repo.save(provider);

  return c.json({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  });
});

app.delete('/api/providers/:id', async (c) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: c.req.param('id') } });
  if (!provider) {
    return c.json({ message: 'Not found' }, 404);
  }

  await repo.remove(provider);
  return c.json({ success: true });
});

export default app;
