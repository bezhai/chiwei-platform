// 同一条飞书消息在整个集群里一次只有一个处理者。
//
// 为什么需要：一个群里可以有多个我们自己的 bot，飞书会把同一条消息推给每一个。
// 不串行的话，每个 bot 都会走一遍投影、各铸一个 id、抢着写。
//
// 这里最要紧的两条不是 happy path：
//   1. 持有者崩了之后，等着的人必须能接手 —— 等待上限要盖得住租约。
//   2. 任务跑得比租约长时不能掉锁 —— 要续租。

import { describe, expect, it } from 'bun:test';

import {
    LARK_MESSAGE_LOCK,
    larkMessageLock,
    redisMessageLockStore,
    type LockHeartbeat,
    type MessageLockRedis,
    type MessageLockStore,
} from './message-lock';

// ---------------------------------------------------------------------------
// 带 TTL 的内存锁存储。租约到期靠注入的时钟判定，所以"崩溃后接手"可以确定性地演。
// ---------------------------------------------------------------------------

interface Lease {
    token: string;
    expiresAt: number;
}

function lockStore(clock: { now: number }): MessageLockStore & { leases: Map<string, Lease> } {
    const leases = new Map<string, Lease>();
    const alive = (key: string): Lease | null => {
        const lease = leases.get(key);
        if (!lease) return null;
        if (lease.expiresAt <= clock.now) {
            leases.delete(key);
            return null;
        }
        return lease;
    };
    return {
        leases,
        async acquire(key, token, leaseSeconds) {
            if (alive(key)) return false;
            leases.set(key, { token, expiresAt: clock.now + leaseSeconds * 1000 });
            return true;
        },
        async renew(key, token, leaseSeconds) {
            const lease = alive(key);
            if (!lease || lease.token !== token) return false;
            lease.expiresAt = clock.now + leaseSeconds * 1000;
            return true;
        },
        async release(key, token) {
            if (alive(key)?.token === token) leases.delete(key);
        },
        async holder(key) {
            return alive(key)?.token ?? null;
        },
    };
}

/** 心跳不真的走时钟：测试自己决定什么时候 tick。 */
function heartbeats() {
    const ticks: Array<() => void> = [];
    let stopped = 0;
    return {
        stopped: () => stopped,
        fire: () => [...ticks].forEach((tick) => tick()),
        schedule: (_everyMs: number, tick: () => void): LockHeartbeat => {
            ticks.push(tick);
            return {
                stop: () => {
                    stopped += 1;
                    const at = ticks.indexOf(tick);
                    if (at >= 0) ticks.splice(at, 1);
                },
            };
        },
    };
}

function wired(overrides: { clock?: { now: number }; onTick?: (now: number) => void } = {}) {
    const clock = overrides.clock ?? { now: 0 };
    const store = lockStore(clock);
    const beats = heartbeats();
    const lock = larkMessageLock(store, {
        now: () => clock.now,
        // 等锁时把时钟推进等的那么久 —— 租约到期就是这样被"等到"的。
        // onTick 是"这段时间里别的进程干了什么"（续租、易主）的注入点：等待者只在
        // sleep 之后才会再看一眼锁，所以别人的动作发生在这里就够真了。
        sleep: async (ms) => {
            clock.now += ms;
            overrides.onTick?.(clock.now);
        },
        newToken: () => 'mine',
        schedule: beats.schedule,
    });
    return { lock, store, beats, clock, key: 'lock:lark:message-projection:om_1' };
}

/** 一个"每 renewEveryMs 续一次租、到 crashAt 就断气"的持有者。 */
function holderRenewingUntil(
    store: ReturnType<typeof lockStore>,
    key: string,
    token: string,
    crashAt: number,
): (now: number) => void {
    let lastRenewAt = 0;
    store.leases.set(key, { token, expiresAt: LARK_MESSAGE_LOCK.leaseSeconds * 1000 });
    return (now: number) => {
        if (now > crashAt) return; // 崩了就没人再续了
        if (now - lastRenewAt < LARK_MESSAGE_LOCK.renewEveryMs) return;
        lastRenewAt = now;
        store.leases.set(key, {
            token,
            expiresAt: now + LARK_MESSAGE_LOCK.leaseSeconds * 1000,
        });
    };
}

/** 让已经排上队的 microtask 跑完（续租是异步的）。 */
async function settle(): Promise<void> {
    for (let i = 0; i < 4; i += 1) await Promise.resolve();
}

// ---------------------------------------------------------------------------

describe('等待上限与租约的关系', () => {
    // codex 指出的破口：租约 120s、等待上限 60s 的话，持有者崩溃时同批到达的其他
    // bot 会在旧锁过期**之前**全部放弃 —— 而飞书入口早就 ACK 过了，那条消息就此
    // 消失。等待上限必须严格盖得住租约。
    it('等待上限严格大于租约', () => {
        expect(LARK_MESSAGE_LOCK.waitTimeoutMs).toBeGreaterThan(
            LARK_MESSAGE_LOCK.leaseSeconds * 1000,
        );
    });

    it('续租间隔明显短于租约，不然续租赶不上过期', () => {
        expect(LARK_MESSAGE_LOCK.renewEveryMs * 2).toBeLessThan(
            LARK_MESSAGE_LOCK.leaseSeconds * 1000,
        );
    });

    // 续租一加进来，"进入时刻 + 固定窗口"就盖不住锁了：持有者可以在 t=40s 续到
    // t=70s、t=41s 崩掉，锁的残留寿命比一个租约还长。等待者按进场那一刻算的 45s
    // 窗口在 t=45s 就散了 —— 飞书直连入口早已 ACK，这条消息就此消失。
    it('持有者续到 70s 才崩：残留寿命超过一个租约，等待者照样接得住', async () => {
        const clock = { now: 0 };
        let holderTick: ((now: number) => void) | undefined;
        const { lock, store, key } = wired({ clock, onTick: (now) => holderTick?.(now) });
        holderTick = holderRenewingUntil(store, key, 'holder', 41_000);

        await expect(lock('om_1', async () => 'taken over')).resolves.toBe('taken over');
        // t=40s 那次续租把锁续到了 t=70s，接手只可能发生在那一刻
        expect(clock.now).toBe(70_000);
    });

    // 反过来的护栏：等不下去这件事必须仍然会发生。持有者一直活着一直续的话，等待者
    // 看到的东西跟"崩溃残留"一模一样（GET 只看得到 token，看不到续租），所以窗口不能
    // 改成"锁还在就永远等" —— 那会把消费者的 prefetch 占死。
    it('持有者一直活着续租时，等待者在有界的时刻放弃', async () => {
        const clock = { now: 0 };
        let holderTick: ((now: number) => void) | undefined;
        const { lock, store, key } = wired({ clock, onTick: (now) => holderTick?.(now) });
        holderTick = holderRenewingUntil(store, key, 'holder', Number.MAX_SAFE_INTEGER);

        await expect(lock('om_1', async () => 'never')).rejects.toThrow('om_1');
        expect(clock.now).toBe(LARK_MESSAGE_LOCK.waitTimeoutMs);
    });

    // 窗口从"最后一次看到锁动了"算，不从进场算：token 变了（易主）说明系统在推进，
    // 凭什么让排在后面的 bot 在队伍还在动的时候放弃。
    it('锁一直在易主时等待窗口跟着重置，等待者不会在队伍还在动时放弃', async () => {
        const clock = { now: 0 };
        const handOffEvery = LARK_MESSAGE_LOCK.leaseSeconds * 1000;
        const lastHandOffAt = handOffEvery * 3;
        const { lock, store, key } = wired({
            clock,
            onTick: (now) => {
                // 上一手正好在这一刻过期，下一手立刻接上：等待者只看得到 token 变了
                if (now > lastHandOffAt || now % handOffEvery !== 0) return;
                store.leases.set(key, {
                    token: `holder-${now / handOffEvery}`,
                    expiresAt: now + handOffEvery,
                });
            },
        });
        store.leases.set(key, { token: 'holder-0', expiresAt: handOffEvery });

        await expect(lock('om_1', async () => 'taken over')).resolves.toBe('taken over');
        // 最后一手在 t=120s 撒手，等待者一路等到了那时候 —— 远超一个窗口
        expect(clock.now).toBe(lastHandOffAt + handOffEvery);
        expect(clock.now).toBeGreaterThan(LARK_MESSAGE_LOCK.waitTimeoutMs);
    });

    it('持有者崩了之后，等着的人在租约到期时接手', async () => {
        const clock = { now: 0 };
        const { lock, store, key } = wired({ clock });
        // 崩掉的持有者留下的锁：没人会来还，只能等它过期
        store.leases.set(key, {
            token: 'crashed',
            expiresAt: LARK_MESSAGE_LOCK.leaseSeconds * 1000,
        });

        await expect(lock('om_1', async () => 'taken over')).resolves.toBe('taken over');
        // 接手发生在等待上限之内 —— 这正是上面那条不等式在保证的事
        expect(clock.now).toBeLessThan(LARK_MESSAGE_LOCK.waitTimeoutMs);
    });
});

describe('续租', () => {
    it('任务还在跑就一直续，租约不会中途到期', async () => {
        const { lock, store, beats, clock, key } = wired();
        const runFor = LARK_MESSAGE_LOCK.leaseSeconds * 1000 * 3;

        await lock('om_1', async () => {
            for (let elapsed = 0; elapsed < runFor; elapsed += LARK_MESSAGE_LOCK.renewEveryMs) {
                clock.now += LARK_MESSAGE_LOCK.renewEveryMs;
                beats.fire();
                await settle();
                expect(store.leases.get(key)?.token).toBe('mine');
            }
        });
    });

    it('任务结束就停止续租，不留后台定时器', async () => {
        const { lock, beats } = wired();
        await lock('om_1', async () => 'done');
        expect(beats.stopped()).toBe(1);
    });

    it('任务抛错也停止续租', async () => {
        const { lock, beats } = wired();
        await expect(lock('om_1', async () => Promise.reject(new Error('boom')))).rejects.toThrow(
            'boom',
        );
        expect(beats.stopped()).toBe(1);
    });

    // 续租失败 = 锁已经不是我们的了 = 互斥在这一刻已经破了。中途取消一个正在写库的
    // 任务比让它跑完更糟，所以继续跑，但要吼出来 —— 否则这件事完全不可观测。
    it('锁已经是别人的时停止续租并吼一声', async () => {
        const { lock, store, beats, key } = wired();
        const shouted: string[] = [];
        const realError = console.error;
        console.error = (...args: unknown[]) => void shouted.push(args.join(' '));

        try {
            await lock('om_1', async () => {
                store.leases.set(key, {
                    token: 'somebody-else',
                    expiresAt: Number.MAX_SAFE_INTEGER,
                });
                beats.fire();
                await settle();
            });
        } finally {
            console.error = realError;
        }

        expect(shouted).toHaveLength(1);
        expect(shouted[0]).toContain('om_1');
        // 停两次：一次是发现掉锁，一次是任务结束
        expect(beats.stopped()).toBe(2);
    });
});

describe('取锁与还锁', () => {
    it('拿到锁才跑，跑完就还回去', async () => {
        const { lock, store } = wired();

        const result = await lock('om_1', async () => {
            expect([...store.leases.keys()]).toEqual(['lock:lark:message-projection:om_1']);
            return 'done';
        });

        expect(result).toBe('done');
        expect(store.leases.size).toBe(0);
    });

    it('任务抛错也要还锁，否则这条消息在租约到期前谁也处理不了', async () => {
        const { lock, store } = wired();

        await expect(lock('om_1', async () => Promise.reject(new Error('boom')))).rejects.toThrow(
            'boom',
        );

        expect(store.leases.size).toBe(0);
    });

    // 等不到就抛错，而不是无限等下去 —— 无限等会把消费者的 prefetch 占死。抛出去
    // 之后：泳道那条路会 requeue 重试（不丢），飞书直连那两个入口只能记错误日志
    // （入口早就 ACK 过了）。所以报错必须说得清楚，不能是"静默放弃"。
    it('等超时抛错，且错误里说得出等了多久', async () => {
        const clock = { now: 0 };
        const { lock, store, key } = wired({ clock });
        // 一直有人活着持有（每次检查都还没过期）
        store.leases.set(key, { token: 'busy', expiresAt: Number.MAX_SAFE_INTEGER });

        await expect(lock('om_1', async () => 'never')).rejects.toThrow(
            new RegExp(`om_1.*${LARK_MESSAGE_LOCK.waitTimeoutMs}ms`),
        );
    });

    it('还锁失败不影响任务的结果', async () => {
        const clock = { now: 0 };
        const store = lockStore(clock);
        store.release = async () => {
            throw new Error('redis went away');
        };
        const lock = larkMessageLock(store, {
            now: () => clock.now,
            sleep: async () => {},
            schedule: heartbeats().schedule,
        });

        await expect(lock('om_1', async () => 'ok')).resolves.toBe('ok');
    });
});

// ---------------------------------------------------------------------------
// Redis 那一侧：三个动作各自的原子性靠什么保证
// ---------------------------------------------------------------------------

describe('redisMessageLockStore', () => {
    function redisDouble() {
        const calls: Array<{ command: string; args: unknown[] }> = [];
        const redis: MessageLockRedis = {
            async get(key) {
                calls.push({ command: 'get', args: [key] });
                return 'whoever';
            },
            async setNx(key, value, seconds) {
                calls.push({ command: 'setNx', args: [key, value, seconds] });
                return 'OK';
            },
            async evalScript(script, numKeys, ...rest) {
                calls.push({ command: 'eval', args: [script, numKeys, ...rest] });
                return 1;
            },
        };
        return { redis, calls };
    }

    it('抢锁是一次 SET NX EX，不是先查再写', async () => {
        const { redis, calls } = redisDouble();
        const store = redisMessageLockStore(() => redis);

        expect(await store.acquire('k', 't', 30)).toBe(true);
        expect(calls).toEqual([{ command: 'setNx', args: ['k', 't', 30] }]);
    });

    it('抢不到时返回 false', async () => {
        const { redis } = redisDouble();
        redis.setNx = async () => null;
        const store = redisMessageLockStore(() => redis);

        expect(await store.acquire('k', 't', 30)).toBe(false);
    });

    // 无条件 EXPIRE / DEL 会把别人的锁续上或者删掉。两个脚本都必须先比对 token。
    it('续租与还锁都先比对 token 再动手', async () => {
        const { redis, calls } = redisDouble();
        const store = redisMessageLockStore(() => redis);

        await store.renew('k', 't', 30);
        await store.release('k', 't');

        expect(calls).toHaveLength(2);
        for (const call of calls) {
            expect(String(call.args[0])).toContain('GET');
            expect(String(call.args[0])).toContain('ARGV[1]');
        }
        expect(String(calls[0]!.args[0])).toContain('EXPIRE');
        expect(calls[0]!.args.slice(1)).toEqual([1, 'k', 't', 30]);
        expect(String(calls[1]!.args[0])).toContain('DEL');
        expect(calls[1]!.args.slice(1)).toEqual([1, 'k', 't']);
    });

    // 看"谁在持有"只是一次观察，不做决定，所以是裸 GET 而不是脚本。
    it('看持有者是一次 GET', async () => {
        const { redis, calls } = redisDouble();
        const store = redisMessageLockStore(() => redis);

        expect(await store.holder('k')).toBe('whoever');
        expect(calls).toEqual([{ command: 'get', args: ['k'] }]);
    });

    it('没人持有时看到 null', async () => {
        const { redis } = redisDouble();
        redis.get = async () => null;
        const store = redisMessageLockStore(() => redis);

        expect(await store.holder('k')).toBeNull();
    });

    it('续租脚本返回 0（锁已易主）时如实报 false', async () => {
        const { redis } = redisDouble();
        redis.evalScript = async () => 0;
        const store = redisMessageLockStore(() => redis);

        expect(await store.renew('k', 't', 30)).toBe(false);
    });
});
