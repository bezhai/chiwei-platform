import { afterEach, describe, expect, it } from 'bun:test';
import type { EventDispatcher } from '@larksuiteoapi/node-sdk';

import { createLarkEventSink } from './event-sink';
import type { LarkEvent } from './lark-event';
import {
    holdsLarkWebSockets,
    openLarkWebSockets,
    type LarkWebSocketBot,
    type LarkWebSocketClient,
    type LarkWebSocketState,
} from './websocket';

const credentials = {
    app_id: 'cli_1',
    app_secret: 'secret',
    encrypt_key: '',
    verification_token: 'vtok',
    robot_union_id: 'on_bot',
};

function fakeClients() {
    const started: Array<{ appId: string; dispatcher: EventDispatcher }> = [];
    const closed: string[] = [];
    const refuse = new Set<string>();
    // SDK 的 start() 只是发起重连，不等连上 —— 所以默认状态是 connecting，测试要
    // 显式把某个 app 标成连上了才算连上。
    const states = new Map<string, LarkWebSocketState>();

    const create = (creds: typeof credentials): LarkWebSocketClient => ({
        start: async ({ eventDispatcher }) => {
            if (refuse.has(creds.app_id)) throw new Error(`${creds.app_id} refused`);
            started.push({ appId: creds.app_id, dispatcher: eventDispatcher });
        },
        close: () => void closed.push(creds.app_id),
        state: () => states.get(creds.app_id) ?? 'connecting',
    });

    return { create, started, closed, refuse, states };
}

function bot(botName: string, appId: string, delivered: LarkEvent[]): LarkWebSocketBot {
    return {
        botName,
        credentials: { ...credentials, app_id: appId },
        sink: createLarkEventSink({
            botName,
            record: async () => {},
            deliver: async (e) => void delivered.push(e),
        }),
    };
}

describe('openLarkWebSockets', () => {
    it('opens one connection per bot', async () => {
        const clients = fakeClients();
        const delivered: LarkEvent[] = [];

        await openLarkWebSockets(
            [bot('chiwei', 'cli_a', delivered), bot('utility', 'cli_b', delivered)],
            clients.create,
        );

        expect(clients.started.map((s) => s.appId).sort()).toEqual(['cli_a', 'cli_b']);
    });

    // 每条长连拿到的必须是**自己那个 bot** 的 dispatcher。接错了的症状是消息处理时
    // bot 身份张冠李戴，而且只在多 bot 部署下才出现。
    it('wires each connection to its own bot', async () => {
        const clients = fakeClients();
        const delivered: LarkEvent[] = [];

        await openLarkWebSockets(
            [bot('chiwei', 'cli_a', delivered), bot('utility', 'cli_b', delivered)],
            clients.create,
        );

        const utility = clients.started.find((s) => s.appId === 'cli_b')!;
        await (utility.dispatcher as unknown as { invoke(d: unknown): Promise<unknown> }).invoke(
            Object.assign(Object.create({ headers: {} }), {
                schema: '2.0',
                header: {
                    event_id: 'e1',
                    token: 'vtok',
                    create_time: '1',
                    event_type: 'im.message.receive_v1',
                    app_id: 'cli_b',
                },
                event: { message: { message_id: 'om_1' } },
            }),
        );
        await Bun.sleep(1);

        expect(delivered.map((e) => e.botName)).toEqual(['utility']);
    });

    // 一个 bot 的凭据过期不该把其他 bot 一起拖下水。
    it('keeps the other bots online when one refuses to connect', async () => {
        const clients = fakeClients();
        clients.refuse.add('cli_a');
        const delivered: LarkEvent[] = [];

        await openLarkWebSockets(
            [bot('chiwei', 'cli_a', delivered), bot('utility', 'cli_b', delivered)],
            clients.create,
        );

        expect(clients.started.map((s) => s.appId)).toEqual(['cli_b']);
    });

    it('closes only the connections that actually came up', async () => {
        const clients = fakeClients();
        clients.refuse.add('cli_a');
        const sockets = await openLarkWebSockets(
            [bot('chiwei', 'cli_a', []), bot('utility', 'cli_b', [])],
            clients.create,
        );

        sockets.close();
        expect(clients.closed).toEqual(['cli_b']);
    });

    it('is safe to close twice', async () => {
        const clients = fakeClients();
        const sockets = await openLarkWebSockets([bot('chiwei', 'cli_a', [])], clients.create);

        sockets.close();
        sockets.close();
        expect(clients.closed).toEqual(['cli_a']);
    });
});

describe('the readiness a long connection reports', () => {
    // 这是切流判据的地基。SDK 的 start() 只是**异步发起**重连，不等首次连接成功，
    // 所以"openLarkWebSockets 返回了"完全不代表我们在接飞书事件。拿它当就绪信号，
    // 切流时就可能在新服务根本没连上的情况下把旧的停掉 —— 那段时间飞书消息无人接收，
    // 而且没有任何告警。
    it('does not call itself connected just because start() returned', async () => {
        const clients = fakeClients();
        const sockets = await openLarkWebSockets([bot('chiwei', 'cli_a', [])], clients.create);

        expect(clients.started).toHaveLength(1); // start() 确实回来了
        expect(sockets.status()).toEqual({
            expected: 1,
            connected: 0,
            bots: [{ botName: 'chiwei', state: 'connecting' }],
        });
    });

    it('reports connected once the socket is actually open', async () => {
        const clients = fakeClients();
        const sockets = await openLarkWebSockets([bot('chiwei', 'cli_a', [])], clients.create);

        clients.states.set('cli_a', 'connected');
        expect(sockets.status()).toEqual({
            expected: 1,
            connected: 1,
            bots: [{ botName: 'chiwei', state: 'connected' }],
        });
    });

    it('follows the socket back down when it drops', async () => {
        const clients = fakeClients();
        const sockets = await openLarkWebSockets([bot('chiwei', 'cli_a', [])], clients.create);

        clients.states.set('cli_a', 'connected');
        expect(sockets.status().connected).toBe(1);
        clients.states.set('cli_a', 'disconnected');
        expect(sockets.status().connected).toBe(0);
    });

    // 一个 bot 连上了不代表入口就位：另一个 bot 的消息还是没人接。
    it('counts every bot it was supposed to connect, not just the ones that worked', async () => {
        const clients = fakeClients();
        clients.refuse.add('cli_a');
        const sockets = await openLarkWebSockets(
            [bot('chiwei', 'cli_a', []), bot('utility', 'cli_b', [])],
            clients.create,
        );
        clients.states.set('cli_b', 'connected');

        const status = sockets.status();
        expect(status.expected).toBe(2);
        expect(status.connected).toBe(1);
        expect(status.bots).toContainEqual({ botName: 'chiwei', state: 'disconnected' });
    });

    it('reports nothing expected when there are no long-connection bots', async () => {
        const sockets = await openLarkWebSockets([], fakeClients().create);
        expect(sockets.status()).toEqual({ expected: 0, connected: 0, bots: [] });
    });

    it('goes back to disconnected after it is closed', async () => {
        const clients = fakeClients();
        const sockets = await openLarkWebSockets([bot('chiwei', 'cli_a', [])], clients.create);
        clients.states.set('cli_a', 'connected');

        sockets.close();
        expect(sockets.status().connected).toBe(0);
    });
});

describe('holdsLarkWebSockets', () => {
    const saved = { ...process.env };
    afterEach(() => {
        process.env = { ...saved };
    });

    // 飞书对同一个 app_id 的多个长连是**随机投递**，不是广播。第二个进程一连上去
    // 就会静默分走一半线上事件 —— 泳道部署尤其危险，因为它看起来只是"多开了一个
    // 测试环境"。所以泳道维度是硬约束，没有 env 后门。
    it('never holds the connection outside the prod deployment', () => {
        process.env.LARK_DIRECT_INGRESS = 'true';
        process.env.LANE = 'ppe-x';
        expect(holdsLarkWebSockets()).toBe(false);
    });

    // env 维度的语义是"prod 里入站走不走长连"的回退开关：webhook 是被动路由，
    // 进程起来就注册；长连是主动的，要显式打开。
    it('waits for the switch to be turned on even in prod', () => {
        delete process.env.LANE;
        delete process.env.LARK_DIRECT_INGRESS;
        expect(holdsLarkWebSockets()).toBe(false);

        process.env.LARK_DIRECT_INGRESS = 'false';
        expect(holdsLarkWebSockets()).toBe(false);
    });

    it('holds the connection in prod once the switch is on', () => {
        delete process.env.LANE;
        process.env.LARK_DIRECT_INGRESS = 'true';
        expect(holdsLarkWebSockets()).toBe(true);
    });
});
