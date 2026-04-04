import { Hono } from 'hono';
import jwt from 'jsonwebtoken';

const app = new Hono();

app.post('/api/auth/login', async (c) => {
  const { password } = (await c.req.json()) as { password?: string };

  if (!password || password !== process.env.DASHBOARD_ADMIN_PASSWORD) {
    return c.json({ message: 'Invalid password' }, 401);
  }

  const secret = process.env.DASHBOARD_JWT_SECRET!;
  const token = jwt.sign({ role: 'admin' }, secret, { expiresIn: '7d' });

  return c.json({ token });
});

export default app;
