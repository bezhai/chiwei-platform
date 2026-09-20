import { describe, expect, it } from 'bun:test';

// AppDataSource 是模块顶层的常量，连接参数在 import 那一刻就读完了。所以
// 「配了 POSTGRES_PORT 用配的值」和「没配用 5432」两种情况没法在同一个进程里
// 都验一遍——模块只会被求值一次。每个用例起一个子进程，import 真正的 db.ts，
// 把 DataSource 的 options 打印出来，验的就是线上启动时拿到的那份参数。
const PROBE =
  "import { AppDataSource } from './src/db.ts';" +
  'const o = AppDataSource.options;' +
  'console.log(JSON.stringify({ host: o.host, port: o.port, username: o.username, database: o.database }));';

const APP_DIR = `${import.meta.dir}/..`;

async function readDataSourceOptions(
  overrides: Record<string, string | null>,
): Promise<{ host: string; port: number; username: string; database: string }> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined && !key.startsWith('POSTGRES_')) {
      env[key] = value;
    }
  }
  for (const [key, value] of Object.entries(overrides)) {
    if (value !== null) {
      env[key] = value;
    }
  }

  const proc = Bun.spawn(['bun', '-e', PROBE], {
    cwd: APP_DIR,
    env,
    stdout: 'pipe',
    stderr: 'pipe',
  });
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]);
  if (exitCode !== 0) {
    throw new Error(`probe failed (exit ${exitCode}): ${stderr}`);
  }
  return JSON.parse(stdout.trim());
}

describe('AppDataSource 的 PostgreSQL 连接参数', () => {
  it('配了 POSTGRES_PORT 就用配的端口', async () => {
    const options = await readDataSourceOptions({
      POSTGRES_HOST: '10.0.0.1',
      POSTGRES_PORT: '5433',
      POSTGRES_USER: 'chiwei_test',
      POSTGRES_DB: 'chiwei_test',
    });

    expect(options.port).toBe(5433);
    // 端口跟同一份配置里的其余几项来自同一处，一起验证避免只有端口被单独接上。
    expect(options.host).toBe('10.0.0.1');
    expect(options.username).toBe('chiwei_test');
    expect(options.database).toBe('chiwei_test');
  });

  it('没配 POSTGRES_PORT 时回落到 5432', async () => {
    const options = await readDataSourceOptions({
      POSTGRES_HOST: '10.0.0.1',
      POSTGRES_PORT: null,
    });

    expect(options.port).toBe(5432);
  });

  it('POSTGRES_PORT 是空串时回落到 5432', async () => {
    const options = await readDataSourceOptions({
      POSTGRES_HOST: '10.0.0.1',
      POSTGRES_PORT: '',
    });

    expect(options.port).toBe(5432);
  });
});
