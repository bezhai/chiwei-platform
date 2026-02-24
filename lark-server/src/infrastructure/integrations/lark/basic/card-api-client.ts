
import { sendReq, reply, send } from '@lark-client';
import { v4 as uuidv4 } from 'uuid';
import { AddElementType } from 'types/lark';
import { CardElement, LarkCard } from 'feishu-card';

export class CardApiClient {
    public async createCard(card: LarkCard): Promise<string | undefined> {
        return sendReq<{
            card_id: string;
        }>(
            '/open-apis/cardkit/v1/cards',
            {
                type: 'card_json',
                data: JSON.stringify(card),
                uuid: uuidv4(),
            },
            'POST',
        ).then((res) => res?.card_id);
    }

    public async updateCard(cardId: string, card: LarkCard, sequence: number): Promise<void> {
        await sendReq(
            `/open-apis/cardkit/v1/cards/${cardId}`,
            {
                card: {
                    type: 'card_json',
                    data: JSON.stringify(card),
                },
                uuid: uuidv4(),
                sequence: sequence,
            },
            'PUT',
        );
    }

    public async replyToMessage(messageId: string, cardId: string): Promise<string | undefined> {
        const realCardJson = {
            type: 'card',
            data: {
                card_id: cardId,
            },
        };
        const sendResp = await reply(messageId, realCardJson, 'interactive');
        return sendResp?.message_id;
    }

    public async sendToChat(chatId: string, cardId: string): Promise<string | undefined> {
        const realCardJson = {
            type: 'card',
            data: {
                card_id: cardId,
            },
        };
        const sendResp = await send(chatId, realCardJson, 'interactive');
        return sendResp?.message_id;
    }

    public async streamUpdateText(cardId: string, elementId: string, content: string, sequence: number) {
        return sendReq(
            `/open-apis/cardkit/v1/cards/${cardId}/elements/${elementId}/content`,
            {
                content,
                sequence: sequence,
                uuid: uuidv4(),
            },
            'PUT',
        );
    }

    public async addElements(
        cardId: string,
        type: AddElementType,
        elements: CardElement[],
        sequence: number,
        targetElementId?: string,
    ): Promise<void> {
        await sendReq(
            `/open-apis/cardkit/v1/cards/${cardId}/elements`,
            {
                type,
                target_element_id: targetElementId,
                elements: JSON.stringify(elements),
                sequence: sequence,
                uuid: uuidv4(),
            },
            'POST',
        );
    }

    public async deleteElement(cardId: string, elementId: string, sequence: number): Promise<void> {
        await sendReq(
            `/open-apis/cardkit/v1/cards/${cardId}/elements/${elementId}`,
            {
                sequence: sequence,
                uuid: uuidv4(),
            },
            'DELETE',
        );
    }

    public async updateCardSettings(cardId: string, config: Partial<LarkCard['config']>, sequence: number): Promise<void> {
        await sendReq(
            `/open-apis/cardkit/v1/cards/${cardId}/settings`,
            {
                settings: JSON.stringify({ config }),
                sequence: sequence,
                uuid: uuidv4(),
            },
            'PATCH',
        );
    }
}
