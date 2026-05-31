import { describe, it, expect } from 'bun:test';
import type { BotConfig } from '@entities/bot-config';
import { larkIngressBots } from './lark-ingress-bots';

describe('larkIngressBots', () => {
    it('keeps only lark channel bots and preserves init_type filtering from caller', () => {
        const bots = [
            { bot_name: 'lark-http', channel: 'lark', init_type: 'http' },
            { bot_name: 'qq-http', channel: 'qq', init_type: 'http' },
        ] as BotConfig[];

        expect(larkIngressBots(bots).map((bot) => bot.bot_name)).toEqual(['lark-http']);
    });
});
