import { LarkMention } from 'types/lark';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';

export class MentionUtils {
    static addMentions(mentions: LarkMention[] | undefined): string[] {
        return mentions ? mentions.map((m) => m.id.union_id!) : [];
    }

    /**
     * 提取被 @mention 的 bot 的 app_id 列表（用于 agent-service 路由）
     * 飞书 mention 不提供 bot_info.app_id，通过 union_id 反查 bot config 获取
     */
    static extractBotAppIds(mentions: LarkMention[] | undefined): string[] {
        if (!mentions) return [];
        return mentions
            .filter((m) => m.mentioned_type === 'bot')
            .map((m) => multiBotManager.getBotConfigByUnionId(m.id.union_id!)?.app_id)
            .filter((appId): appId is string => !!appId);
    }

    static addMentionMap(mentions: LarkMention[] | undefined): Record<
        string,
        {
            name: string;
            openId: string;
            appId?: string;
        }
    > {
        return mentions
            ? mentions.reduce(
                  (acc, m) => {
                      const botConfig = m.mentioned_type === 'bot'
                          ? multiBotManager.getBotConfigByUnionId(m.id.union_id!)
                          : null;
                      acc[m.id.union_id!] = {
                          name: m.name,
                          openId: m.id.open_id!,
                          appId: botConfig?.app_id,
                      };
                      return acc;
                  },
                  {} as Record<
                      string,
                      {
                          name: string;
                          openId: string;
                          appId?: string;
                      }
                  >,
              )
            : {};
    }
}
