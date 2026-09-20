import { afterEach, describe, expect, it } from 'bun:test';

import { buildMongoUrl } from './mongo';

const MONGO_KEYS = [
  'MONGO_HOST',
  'MONGO_PORT',
  'MONGO_INITDB_ROOT_USERNAME',
  'MONGO_INITDB_ROOT_PASSWORD',
] as const;

const saved = new Map<string, string | undefined>(
  MONGO_KEYS.map((key) => [key, process.env[key]]),
);

function setEnv(values: Record<string, string | null>) {
  for (const key of MONGO_KEYS) {
    delete process.env[key];
  }
  for (const [key, value] of Object.entries(values)) {
    if (value !== null) {
      process.env[key] = value;
    }
  }
}

afterEach(() => {
  for (const [key, value] of saved) {
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
});

describe('buildMongoUrl', () => {
  it('配了 MONGO_PORT 就用配的端口', () => {
    setEnv({
      MONGO_HOST: '10.0.0.1',
      MONGO_PORT: '27018',
      MONGO_INITDB_ROOT_USERNAME: 'chiwei-test',
      MONGO_INITDB_ROOT_PASSWORD: 'pw',
    });

    expect(buildMongoUrl()).toBe(
      'mongodb://chiwei-test:pw@10.0.0.1:27018/chiwei?connectTimeoutMS=2000&authSource=admin',
    );
  });

  it('没配 MONGO_PORT 时回落到 27017', () => {
    setEnv({
      MONGO_HOST: 'mongodb',
      MONGO_PORT: null,
      MONGO_INITDB_ROOT_USERNAME: 'chiwei',
      MONGO_INITDB_ROOT_PASSWORD: 'pw',
    });

    expect(buildMongoUrl()).toBe(
      'mongodb://chiwei:pw@mongodb:27017/chiwei?connectTimeoutMS=2000&authSource=admin',
    );
  });

  it('MONGO_PORT 是空串时回落到 27017', () => {
    setEnv({
      MONGO_HOST: 'mongodb',
      MONGO_PORT: '',
      MONGO_INITDB_ROOT_USERNAME: 'chiwei',
      MONGO_INITDB_ROOT_PASSWORD: 'pw',
    });

    expect(buildMongoUrl()).toContain('@mongodb:27017/chiwei');
  });
});
