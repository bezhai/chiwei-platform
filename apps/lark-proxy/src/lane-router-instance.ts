import { LaneRouter } from '@inner/shared/lane-router';

export const laneRouter = new LaneRouter(
    process.env.REGISTRY_URL || 'http://lite-registry:8080',
);
