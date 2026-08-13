import { afterEach, describe, expect, it } from 'bun:test';
import { Column, Entity, PrimaryColumn } from 'typeorm';

import {
    bindDataSource,
    boundDataSource,
    createPostgresDataSource,
    resetBoundDataSource,
} from './data-source';

// 共享包不得知道任何具体渠道的表。实体清单必须整份由调用方传入 —— 只要包内
// 偷偷往 entities 里塞一条，每个服务就都会连带加载别的服务独占的表，而这正是
// 拆服务要消灭的耦合。本组用例把「包内零追加」钉死成可回归的断言，而不是靠
// code review 口头保证。

@Entity('fake_alpha')
class FakeAlpha {
    @PrimaryColumn({ type: 'varchar' })
    id!: string;
}

@Entity('fake_beta')
class FakeBeta {
    @PrimaryColumn({ type: 'varchar' })
    id!: string;

    @Column({ type: 'varchar', nullable: true })
    label?: string;
}

afterEach(() => {
    resetBoundDataSource();
});

describe('createPostgresDataSource: 实体注册整份由调用方传入', () => {
    it('传空清单就是空清单 —— 包内不追加任何实体', () => {
        const ds = createPostgresDataSource({ entities: [] });
        expect(ds.options.entities).toEqual([]);
    });

    it('传什么就注册什么，顺序与内容逐项一致', () => {
        const ds = createPostgresDataSource({ entities: [FakeAlpha, FakeBeta] });
        expect(ds.options.entities).toEqual([FakeAlpha, FakeBeta]);
    });

    it('调用方各自组合互不串味（一个 DataSource 的实体不会漏进另一个）', () => {
        const a = createPostgresDataSource({ entities: [FakeAlpha] });
        const b = createPostgresDataSource({ entities: [FakeBeta] });
        expect(a.options.entities).not.toContain(FakeBeta);
        expect(b.options.entities).not.toContain(FakeAlpha);
    });

    it('synchronize 恒为 false —— DDL 只走 /ops-db submit 或 migration', () => {
        const ds = createPostgresDataSource({ entities: [] });
        expect(ds.options.synchronize).toBe(false);
    });

    it('连接参数默认取 POSTGRES_* 环境变量', () => {
        const saved = {
            host: process.env.POSTGRES_HOST,
            port: process.env.POSTGRES_PORT,
            user: process.env.POSTGRES_USER,
            password: process.env.POSTGRES_PASSWORD,
            db: process.env.POSTGRES_DB,
        };
        process.env.POSTGRES_HOST = 'pg.example';
        process.env.POSTGRES_PORT = '6543';
        process.env.POSTGRES_USER = 'u';
        process.env.POSTGRES_PASSWORD = 'p';
        process.env.POSTGRES_DB = 'd';
        try {
            const opts = createPostgresDataSource({ entities: [] }).options as unknown as Record<
                string,
                unknown
            >;
            expect(opts.type).toBe('postgres');
            expect(opts.host).toBe('pg.example');
            expect(opts.port).toBe(6543);
            expect(opts.username).toBe('u');
            expect(opts.password).toBe('p');
            expect(opts.database).toBe('d');
        } finally {
            restoreEnv('POSTGRES_HOST', saved.host);
            restoreEnv('POSTGRES_PORT', saved.port);
            restoreEnv('POSTGRES_USER', saved.user);
            restoreEnv('POSTGRES_PASSWORD', saved.password);
            restoreEnv('POSTGRES_DB', saved.db);
        }
    });

    it('POSTGRES_PORT 缺失时回落 5432', () => {
        const saved = process.env.POSTGRES_PORT;
        delete process.env.POSTGRES_PORT;
        try {
            const opts = createPostgresDataSource({ entities: [] }).options as unknown as Record<
                string,
                unknown
            >;
            expect(opts.port).toBe(5432);
        } finally {
            restoreEnv('POSTGRES_PORT', saved);
        }
    });
});

describe('bindDataSource: 组装根绑定，包内代码只读绑定值', () => {
    it('未绑定就取用 —— fail-closed 抛错，不返回 undefined 让调用方裸奔', () => {
        expect(() => boundDataSource()).toThrow(/no DataSource is bound/i);
    });

    it('绑定后取回的是同一个实例', () => {
        const ds = createPostgresDataSource({ entities: [FakeAlpha] });
        bindDataSource(ds);
        expect(boundDataSource()).toBe(ds);
    });

    it('重复绑定不同实例 fail-closed —— 静默顶掉会让两套连接池同时在跑', () => {
        const a = createPostgresDataSource({ entities: [FakeAlpha] });
        const b = createPostgresDataSource({ entities: [FakeBeta] });
        bindDataSource(a);
        expect(() => bindDataSource(b)).toThrow(/already bound/i);
        // 同一实例重复绑定是幂等的（模块被多次求值时不该炸）
        expect(() => bindDataSource(a)).not.toThrow();
    });
});

function restoreEnv(key: string, value: string | undefined): void {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
}
