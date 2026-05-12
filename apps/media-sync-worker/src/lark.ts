import { createLarkClient, LarkClient } from "@inner/lark-utils";

// 创建 Lark 客户端实例
const larkClient: LarkClient = createLarkClient({
  appId: process.env.APP_ID!,
  appSecret: process.env.APP_SECRET!,
});

export async function send_msg(chat_id: string, message: string) {
  try {
    await larkClient.sendText(chat_id, message);
  } catch (e: any) {
    console.error("Error sending message:", e.message || e);
  }
}

export async function send_card(chat_id: string, card: any) {
  try {
    await larkClient.sendCard(chat_id, card);
  } catch (e: any) {
    console.error("Error sending card:", e.message || e);
  }
}

// 导出 Lark 客户端实例，以便其他模块使用
export { larkClient };
