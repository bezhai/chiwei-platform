import { LaneRouter } from '@inner/shared';
import { register } from '@middleware/metrics';

export const laneRouter = new LaneRouter(
    process.env.REGISTRY_URL || 'http://lite-registry:8080',
    30_000,
    register,
);
