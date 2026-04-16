import { Hono } from 'hono';
import fs from 'node:fs/promises';
import path from 'node:path';

const SKILLS_DIR = process.env.SKILLS_DIR || '/data/skills';

const app = new Hono();

/** Extract description from YAML frontmatter: ---\ndescription: xxx\n--- */
function parseDescription(content: string): string {
  const match = content.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!match) return '';
  const frontmatter = match[1];
  const descMatch = frontmatter.match(/^description:\s*(.+)$/m);
  return descMatch ? descMatch[1].trim() : '';
}

/** Recursively list all files with relative paths under dir */
async function listFilesRecursive(dir: string, base: string): Promise<string[]> {
  const results: string[] = [];
  let entries: import('node:fs').Dirent[];
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch {
    return results;
  }
  for (const entry of entries) {
    const rel = base ? `${base}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      const nested = await listFilesRecursive(path.join(dir, entry.name), rel);
      results.push(...nested);
    } else {
      results.push(rel);
    }
  }
  return results;
}

/** Prevent path traversal: resolved path must start with SKILLS_DIR */
function safeResolve(...parts: string[]): string | null {
  const resolved = path.resolve(SKILLS_DIR, ...parts);
  if (!resolved.startsWith(path.resolve(SKILLS_DIR))) return null;
  return resolved;
}

/** GET /api/skills — List all skills */
app.get('/api/skills', async (c) => {
  let entries: import('node:fs').Dirent[];
  try {
    entries = await fs.readdir(SKILLS_DIR, { withFileTypes: true });
  } catch {
    return c.json([]);
  }

  const skills = await Promise.all(
    entries
      .filter((e) => e.isDirectory())
      .map(async (e) => {
        const skillMdPath = path.join(SKILLS_DIR, e.name, 'SKILL.md');
        let description = '';
        try {
          const content = await fs.readFile(skillMdPath, 'utf-8');
          description = parseDescription(content);
        } catch {
          // No SKILL.md or unreadable — leave description empty
        }
        return { name: e.name, description };
      })
  );

  return c.json(skills);
});

/** GET /api/skills/:name — Get a skill's full content */
app.get('/api/skills/:name', async (c) => {
  const name = c.req.param('name');
  const skillDir = safeResolve(name);
  if (!skillDir) return c.json({ message: 'Invalid skill name' }, 400);

  try {
    await fs.access(skillDir);
  } catch {
    return c.json({ message: 'Skill not found' }, 404);
  }

  const skillMdPath = path.join(skillDir, 'SKILL.md');
  let content = '';
  try {
    content = await fs.readFile(skillMdPath, 'utf-8');
  } catch {
    // SKILL.md may not exist yet
  }

  const files = await listFilesRecursive(skillDir, '');

  return c.json({ name, content, files });
});

/** POST /api/skills — Create new skill */
app.post('/api/skills', async (c) => {
  const body = (await c.req.json()) as { name?: string; content?: string };
  const { name, content } = body;

  if (!name) return c.json({ message: 'name is required' }, 400);

  const skillDir = safeResolve(name);
  if (!skillDir) return c.json({ message: 'Invalid skill name' }, 400);

  try {
    await fs.access(skillDir);
    return c.json({ message: 'Skill already exists' }, 409);
  } catch {
    // Expected — skill doesn't exist yet
  }

  await fs.mkdir(skillDir, { recursive: true });

  if (content !== undefined) {
    await fs.writeFile(path.join(skillDir, 'SKILL.md'), content, 'utf-8');
  }

  return c.json({ name, content: content ?? '' }, 201);
});

/** PUT /api/skills/:name — Update SKILL.md */
app.put('/api/skills/:name', async (c) => {
  const name = c.req.param('name');
  const skillDir = safeResolve(name);
  if (!skillDir) return c.json({ message: 'Invalid skill name' }, 400);

  try {
    await fs.access(skillDir);
  } catch {
    return c.json({ message: 'Skill not found' }, 404);
  }

  const body = (await c.req.json()) as { content?: string };
  if (body.content === undefined) return c.json({ message: 'content is required' }, 400);

  await fs.writeFile(path.join(skillDir, 'SKILL.md'), body.content, 'utf-8');
  return c.json({ name, content: body.content });
});

/** DELETE /api/skills/:name — Delete entire skill directory */
app.delete('/api/skills/:name', async (c) => {
  const name = c.req.param('name');
  const skillDir = safeResolve(name);
  if (!skillDir) return c.json({ message: 'Invalid skill name' }, 400);

  try {
    await fs.access(skillDir);
  } catch {
    return c.json({ message: 'Skill not found' }, 404);
  }

  await fs.rm(skillDir, { recursive: true, force: true });
  return c.json({ success: true });
});

/** GET /api/skills/:name/files/* — Read a specific file within a skill */
app.get('/api/skills/:name/files/*', async (c) => {
  const name = c.req.param('name');
  const filePath = c.req.param('*');
  if (!filePath) return c.json({ message: 'File path is required' }, 400);

  const resolved = safeResolve(name, filePath);
  if (!resolved) return c.json({ message: 'Invalid file path' }, 400);

  try {
    const content = await fs.readFile(resolved, 'utf-8');
    return c.json({ path: filePath, content });
  } catch {
    return c.json({ message: 'File not found' }, 404);
  }
});

/** PUT /api/skills/:name/files/* — Write a specific file within a skill */
app.put('/api/skills/:name/files/*', async (c) => {
  const name = c.req.param('name');
  const filePath = c.req.param('*');
  if (!filePath) return c.json({ message: 'File path is required' }, 400);

  const resolved = safeResolve(name, filePath);
  if (!resolved) return c.json({ message: 'Invalid file path' }, 400);

  const body = (await c.req.json()) as { content?: string };
  if (body.content === undefined) return c.json({ message: 'content is required' }, 400);

  await fs.mkdir(path.dirname(resolved), { recursive: true });
  await fs.writeFile(resolved, body.content, 'utf-8');
  return c.json({ path: filePath, content: body.content });
});

/** DELETE /api/skills/:name/files/* — Delete a specific file within a skill */
app.delete('/api/skills/:name/files/*', async (c) => {
  const name = c.req.param('name');
  const filePath = c.req.param('*');
  if (!filePath) return c.json({ message: 'File path is required' }, 400);

  const resolved = safeResolve(name, filePath);
  if (!resolved) return c.json({ message: 'Invalid file path' }, 400);

  try {
    await fs.unlink(resolved);
    return c.json({ success: true });
  } catch {
    return c.json({ message: 'File not found' }, 404);
  }
});

export default app;
