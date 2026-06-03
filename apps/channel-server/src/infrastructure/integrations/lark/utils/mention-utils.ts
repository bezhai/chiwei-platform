import { LarkMention } from 'types/lark';
import {
    getLarkBotConfigByAppId,
    getLarkBotConfigByUnionId,
    getLarkDisplayNameByAppId,
    larkCredentials,
} from '@plugins/lark/bot-identity';

export class MentionUtils {
    static addMentions(mentions: LarkMention[] | undefined): string[] {
        return mentions ? mentions.map((m) => m.id.union_id!) : [];
    }

    static addMentionMap(mentions: LarkMention[] | undefined): Record<
        string,
        {
            name: string;
            openId: string;
            botCommonUserId?: string;
        }
    > {
        return mentions
            ? mentions.reduce(
                  (acc, m) => {
                      const botConfig =
                          m.mentioned_type === 'bot'
                              ? m.bot_info?.app_id
                                  ? getLarkBotConfigByAppId(m.bot_info.app_id)
                                  : getLarkBotConfigByUnionId(m.id.union_id!)
                              : null;
                      const appId = botConfig ? larkCredentials(botConfig).app_id : undefined;
                      if (botConfig && !botConfig.common_user_id) {
                          throw new Error(
                              `registered bot mention "${botConfig.bot_name}" has no ` +
                                  'common_user_id; bot identity initialization must run ' +
                                  'before Lark mention parsing',
                          );
                      }
                      acc[m.id.union_id!] = {
                          name: appId ? (getLarkDisplayNameByAppId(appId) ?? m.name) : m.name,
                          openId: m.id.open_id!,
                          botCommonUserId: botConfig?.common_user_id,
                      };
                      return acc;
                  },
                  {} as Record<
                      string,
                      {
                          name: string;
                          openId: string;
                          botCommonUserId?: string;
                      }
                  >,
              )
            : {};
    }
}
