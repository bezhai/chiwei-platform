import { replyCard, replyMessage } from '@lark/basic/message';
import { CardHeader, LarkCard, MarkdownComponent, TableColumn, TableComponent } from 'feishu-card';
import { Message } from 'core/models/message';
import { getAIKeyInfo, getBalance } from 'infrastructure/integrations/provider-admin';

export async function sendBalance(message: Message) {
    try {
        const [balance, aiKeyInfo] = await Promise.all([getBalance(), getAIKeyInfo()]);

        const costFormat = (cost: number) => {
            return cost > 0 ? (cost / 1000).toFixed(3) : '-';
        };

        const balanceCard = new LarkCard()
            .withHeader(new CardHeader('302AI使用情况').color('orange'))
            .addElement(
                new MarkdownComponent(`**当前余额：** ${balance.data.balance}`),
                new TableComponent()
                    .addColumn(new TableColumn('api_name').setDisplayName('API名称'))
                    .addColumn(new TableColumn('limit_daily_cost').setDisplayName('每日上限'))
                    .addColumn(new TableColumn('current_date_cost').setDisplayName('今日消耗'))
                    .addColumn(new TableColumn('limit_cost').setDisplayName('消耗总上限'))
                    .addColumn(new TableColumn('current_cost').setDisplayName('当前总消耗'))
                    .appendRows(
                        ...aiKeyInfo.map((item) => ({
                            api_name: item.api_name,
                            limit_daily_cost: costFormat(item.limit_daily_cost),
                            current_date_cost: costFormat(item.current_date_cost),
                            limit_cost: costFormat(item.limit_cost),
                            current_cost: costFormat(item.current_cost),
                        })),
                    ),
            );

        await replyCard(message.messageId, balanceCard);
    } catch (error) {
        console.error('send balance error:', {
            message: error instanceof Error ? error.message : 'Unknown error',
            stack: error instanceof Error ? error.stack : undefined,
        });
        replyMessage(message.messageId, '获取余额信息失败', true);
    }
}
