import { LarkMention } from 'types/lark';
import { ContentType, type ContentItem, type MessageMention } from '@core/models/message-content';
import {
    getLarkBotConfigByAppId,
    getLarkBotConfigByUnionId,
    getLarkDisplayNameByAppId,
    larkCredentials,
} from '@plugins/lark/bot-identity';

export class MentionUtils {
    static addMentions(mentions: LarkMention[] | undefined): MessageMention[] {
        return mentions
            ? mentions.flatMap((m) => {
                  const id = m.id.union_id ?? m.id.user_id ?? m.id.open_id;
                  if (!id) return [];
                  const botConfig =
                      m.mentioned_type === 'bot'
                          ? m.bot_info?.app_id
                              ? getLarkBotConfigByAppId(m.bot_info.app_id)
                              : m.id.union_id
                                ? getLarkBotConfigByUnionId(m.id.union_id)
                                : null
                          : null;
                  const appId = botConfig ? larkCredentials(botConfig).app_id : undefined;
                  const configuredDisplayName = appId
                      ? getLarkDisplayNameByAppId(appId)
                      : undefined;
                  const displayName = configuredDisplayName?.trim() || m.name?.trim() || id;
                  if (botConfig && !botConfig.common_user_id) {
                      throw new Error(
                          `registered bot mention "${botConfig.bot_name}" has no ` +
                              'common_user_id; bot identity initialization must run ' +
                              'before Lark mention parsing',
                      );
                  }
                  return {
                      key: m.key,
                      id,
                      displayName,
                      botCommonUserId: botConfig?.common_user_id,
                  };
              })
            : [];
    }

    static applyMentionTokens(items: ContentItem[], mentions: MessageMention[]): ContentItem[] {
        const mentionByKey = new Map(
            mentions
                .filter((mention) => mention.key)
                .map((mention) => [mention.key as string, mention]),
        );
        return items.flatMap((item) => {
            if (item.type !== ContentType.Text) return [item];

            const out: ContentItem[] = [];
            const tokenPattern = /@_user_\d+/g;
            let lastIndex = 0;
            let match: RegExpExecArray | null;

            while ((match = tokenPattern.exec(item.value)) !== null) {
                if (match.index > lastIndex) {
                    out.push({
                        type: ContentType.Text,
                        value: item.value.slice(lastIndex, match.index),
                    });
                }

                const mention = mentionByKey.get(match[0]);
                if (mention) {
                    out.push({
                        type: ContentType.Mention,
                        value: mention.displayName,
                        meta: {
                            channel_user_id: mention.id,
                            bot_common_user_id: mention.botCommonUserId,
                        },
                    });
                } else {
                    out.push({
                        type: ContentType.Text,
                        value: match[0],
                    });
                }
                lastIndex = match.index + match[0].length;
            }

            if (lastIndex < item.value.length) {
                out.push({
                    type: ContentType.Text,
                    value: item.value.slice(lastIndex),
                });
            }

            return out.length > 0 ? out : [item];
        });
    }
}
