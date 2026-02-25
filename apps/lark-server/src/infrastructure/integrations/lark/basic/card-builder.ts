import {
    Config,
    StreamConfig,
    Summary,
    CollapsiblePanelComponent,
    CollapsiblePanelHeader,
    MarkdownComponent,
    HrComponent,
    ColumnSet,
    InteractiveContainerComponent,
    Column,
    LarkCard,
    StandardIcon,
} from 'feishu-card';
import { LarkCardRetry, LarkCardThumbsDown, LarkCardThumbsUp } from 'types/lark';

export const ELEMENT_IDS = {
    REASONING: 'reasoning_content',
    RESPONSE: 'response_content',
    THINKING_PLACEHOLDER: 'thinking_placeholder',
    HR: 'hr',
    COLLAPSE: 'collapse',
    INTERACTION_BUTTONS: 'interaction_buttons',
    THUMBS_UP_COLUMN: 'thumbs_up_column',
    THUMBS_DOWN_COLUMN: 'thumbs_down_column',
    RETRY_COLUMN: 'retry_column',
    UP_INTERACTIVE: 'up_interactive',
    DOWN_INTERACTIVE: 'down_interactive',
    RETRY_INTERACTIVE: 'retry_interactive',
    THUMBS_UP_TEXT: 'thumbs_up_text',
    THUMBS_DOWN_TEXT: 'thumbs_down_text',
    RETRY_TEXT: 'retry_text',
    ERROR_MESSAGE: 'error_message',
} as const;

export class CardBuilder {
    private card: LarkCard;

    constructor() {
        this.card = new LarkCard();
    }

    public buildInitialCard(): LarkCard {
        this.card.withConfig(
            new Config()
                .withStreamingMode(
                    true,
                    new StreamConfig()
                        .withPrintStrategy('fast')
                        .withPrintFrequency(20)
                        .withPrintStep(4),
                )
                .withSummary(new Summary('少女回复中')),
        );
        return this.card;
    }

    public buildInitialElements() {
        return [
            new HrComponent().setElementId(ELEMENT_IDS.HR),
            new MarkdownComponent('赤尾思考中...').setElementId(ELEMENT_IDS.THINKING_PLACEHOLDER),
        ];
    }

    public buildReasoningElement() {
        return new CollapsiblePanelComponent(
            new CollapsiblePanelHeader('赤尾的内心思考').setBackgroundColor('grey-100'),
        )
            .setElementId(ELEMENT_IDS.COLLAPSE)
            .setBorder('grey-100')
            .pushElement(new MarkdownComponent('').setElementId(ELEMENT_IDS.REASONING));
    }

    public buildResponseElement() {
        return new MarkdownComponent('').setElementId(ELEMENT_IDS.RESPONSE);
    }

    public buildInteractionElements(messageId: string, cardContext: Record<string, any>) {
        const columnSet = new ColumnSet()
            .setHorizontalSpacing('small')
            .setElementId(ELEMENT_IDS.INTERACTION_BUTTONS);

        const thumbsUpColumn = new Column()
            .setElementId(ELEMENT_IDS.THUMBS_UP_COLUMN)
            .addElements(
                new InteractiveContainerComponent()
                    .setElementId(ELEMENT_IDS.UP_INTERACTIVE)
                    .pushElement(
                        new MarkdownComponent('')
                            .setElementId(ELEMENT_IDS.THUMBS_UP_TEXT)
                            .setIcon(new StandardIcon('thumbsup_outlined', 'grey')),
                    )
                    .addCallbackBehavior({
                        type: LarkCardThumbsUp,
                        message_id: messageId,
                        ...cardContext,
                    })
                    .setCornerRadius('2px')
                    .setHorizontalAlign('center')
                    .setPadding('0px'),
            )
            .setWidth('30px');

        const thumbsDownColumn = new Column()
            .setElementId(ELEMENT_IDS.THUMBS_DOWN_COLUMN)
            .addElements(
                new InteractiveContainerComponent()
                    .setElementId(ELEMENT_IDS.DOWN_INTERACTIVE)
                    .pushElement(
                        new MarkdownComponent('')
                            .setElementId(ELEMENT_IDS.THUMBS_DOWN_TEXT)
                            .setIcon(new StandardIcon('thumbdown_outlined', 'grey')),
                    )
                    .addCallbackBehavior({
                        type: LarkCardThumbsDown,
                        message_id: messageId,
                        ...cardContext,
                    })
                    .setCornerRadius('2px')
                    .setHorizontalAlign('center')
                    .setPadding('0px'),
            )
            .setWidth('30px');

        const retryColumn = new Column()
            .setElementId(ELEMENT_IDS.RETRY_COLUMN)
            .addElements(
                new InteractiveContainerComponent()
                    .setElementId(ELEMENT_IDS.RETRY_INTERACTIVE)
                    .pushElement(
                        new MarkdownComponent('')
                            .setElementId(ELEMENT_IDS.RETRY_TEXT)
                            .setIcon(new StandardIcon('refresh_outlined', 'grey')),
                    )
                    .addCallbackBehavior({
                        type: LarkCardRetry,
                        message_id: messageId,
                        ...cardContext,
                    })
                    .setCornerRadius('2px')
                    .setPadding('0px')
                    .setHorizontalAlign('center'),
            )
            .setWidth('30px');

        columnSet.addColumns(thumbsUpColumn, thumbsDownColumn, retryColumn);
        return columnSet;
    }

    public buildErrorElement() {
        return new MarkdownComponent(
            `**<font color='red-500'>赤尾似乎遇到了一些问题，可以重试一下看看！</font>**`,
        ).setElementId(ELEMENT_IDS.ERROR_MESSAGE);
    }

    public static truncate(str: string, max: number): string {
        return str.length > max ? str.slice(0, max) + '...' : str;
    }
}
