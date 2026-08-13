import { afterEach, describe, expect, it } from 'bun:test';
import { isProdDeployment } from './lane-policy';

describe('isProdDeployment', () => {
    it('LANE 未注入 / 空 / prod → prod 部署', () => {
        expect(isProdDeployment(undefined)).toBe(true);
        expect(isProdDeployment('')).toBe(true);
        expect(isProdDeployment('prod')).toBe(true);
    });

    it('任何泳道值 → 非 prod 部署', () => {
        expect(isProdDeployment('ppe-x')).toBe(false);
        expect(isProdDeployment('coe-y')).toBe(false);
        expect(isProdDeployment('blue')).toBe(false);
        expect(isProdDeployment('prod-2')).toBe(false);
    });

    describe('默认读 process.env.LANE', () => {
        const originalLane = process.env.LANE;

        afterEach(() => {
            if (originalLane === undefined) delete process.env.LANE;
            else process.env.LANE = originalLane;
        });

        it('LANE=prod → true，LANE=ppe-x → false，LANE 删掉 → true', () => {
            process.env.LANE = 'prod';
            expect(isProdDeployment()).toBe(true);
            process.env.LANE = 'ppe-x';
            expect(isProdDeployment()).toBe(false);
            delete process.env.LANE;
            expect(isProdDeployment()).toBe(true);
        });
    });
});
