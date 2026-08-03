import { afterEach, describe, it, expect } from 'bun:test';
import { isDirectIngressEnabled } from './ingress-gate';

// 飞书直连长连（WSClient）的开关是「prod 部署 AND env」：
//   - 泳道：飞书对共享 app_id 的多个长连客户端是随机投递而非广播，任何非 prod
//     泳道建长连都会静默分走线上事件，所以泳道一律 off，且不给 env 后门；
//   - env：prod 内部保留 LARK_DIRECT_INGRESS 作为「入站走不走长连」的开关。
describe('isDirectIngressEnabled（LANE + LARK_DIRECT_INGRESS）', () => {
    const originalLane = process.env.LANE;
    const originalFlag = process.env.LARK_DIRECT_INGRESS;

    function setEnv(lane: string | undefined, flag: string | undefined): void {
        if (lane === undefined) delete process.env.LANE;
        else process.env.LANE = lane;
        if (flag === undefined) delete process.env.LARK_DIRECT_INGRESS;
        else process.env.LARK_DIRECT_INGRESS = flag;
    }

    afterEach(() => {
        setEnv(originalLane, originalFlag);
    });

    it("LANE 未注入 + flag 'true' → on（空等价于 prod）", () => {
        setEnv(undefined, 'true');
        expect(isDirectIngressEnabled()).toBe(true);
    });

    it("LANE=prod + flag 'true' → on", () => {
        setEnv('prod', 'true');
        expect(isDirectIngressEnabled()).toBe(true);
    });

    it("LANE=prod 但 flag 未开 / 非 'true' → off（与关系）", () => {
        setEnv('prod', undefined);
        expect(isDirectIngressEnabled()).toBe(false);
        setEnv('prod', 'false');
        expect(isDirectIngressEnabled()).toBe(false);
        setEnv('prod', '1');
        expect(isDirectIngressEnabled()).toBe(false);
    });

    it("LANE=ppe-x + flag 'true' → off（泳道不抢线上长连）", () => {
        setEnv('ppe-x', 'true');
        expect(isDirectIngressEnabled()).toBe(false);
    });

    it("LANE=coe-y + flag 'true' → off", () => {
        setEnv('coe-y', 'true');
        expect(isDirectIngressEnabled()).toBe(false);
    });

    it('泳道 + flag 未开 → off', () => {
        setEnv('ppe-x', undefined);
        expect(isDirectIngressEnabled()).toBe(false);
    });
});
