import { LarkMention } from 'types/lark';

export class MentionUtils {
    static addMentions(mentions: LarkMention[] | undefined): string[] {
        return mentions ? mentions.map((m) => m.id.union_id!) : [];
    }

    /**
     * 提取被 @mention 的 bot 的 app_id 列表（用于 agent-service 路由）
     * 只包含 mentioned_type === 'bot' 的 mention
     */
    static extractBotAppIds(mentions: LarkMention[] | undefined): string[] {
        if (!mentions) return [];
        return mentions
            .filter((m) => m.mentioned_type === 'bot' && m.bot_info?.app_id)
            .map((m) => m.bot_info!.app_id!);
    }

    static addMentionMap(mentions: LarkMention[] | undefined): Record<
        string,
        {
            name: string;
            openId: string;
        }
    > {
        return mentions
            ? mentions.reduce(
                  (acc, m) => {
                      acc[m.id.union_id!] = {
                          name: m.name,
                          openId: m.id.open_id!,
                      };
                      return acc;
                  },
                  {} as Record<
                      string,
                      {
                          name: string;
                          openId: string;
                      }
                  >,
              )
            : {};
    }
}
