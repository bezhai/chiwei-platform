
import { CardContextRepository } from 'infrastructure/dal/repositories/repositories';

export interface CardContext {
    card_id: string;
    message_id: string;
    chat_id: string;
    sequence: number;
    last_updated: Date;
    created_at?: Date;
}

export class CardContextService {
    public async saveContext(context: CardContext): Promise<void> {
        try {
            const existingContext = await CardContextRepository.findOne({
                where: { card_id: context.card_id },
            });

            if (existingContext) {
                await CardContextRepository.update({ card_id: context.card_id }, context);
            } else {
                await CardContextRepository.save({
                    ...context,
                    created_at: new Date(),
                });
            }
        } catch (error) {
            console.error('保存上下文失败:', error);
            throw error;
        }
    }

    public async loadContext(messageId: string): Promise<CardContext | null> {
        const cardContext = await CardContextRepository.findOne({
            where: { message_id: messageId },
        });
        if (!cardContext) {
            return null;
        }
        return cardContext;
    }
}
