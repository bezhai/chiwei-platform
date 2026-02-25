import { LarkMention } from 'types/lark';

export class MentionUtils {
    static addMentions(mentions: LarkMention[] | undefined): string[] {
        return mentions ? mentions.map((m) => m.id.union_id!) : [];
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
