import type { Message } from 'core/models/message';
import { laneRouter } from '@infrastructure/lane-router';

export function enqueueLarkImagePipeline(message: Message, botName: string | undefined): void {
    if (!message.allowDownloadResource()) return;

    const toolClient = laneRouter.createClient('tool-service');
    for (const imageKey of message.imageKeys()) {
        toolClient
            .post(
                '/api/image-pipeline/process',
                { message_id: message.messageId, file_key: imageKey },
                {
                    headers: {
                        Authorization: `Bearer ${process.env.INNER_HTTP_SECRET}`,
                        'X-App-Name': botName,
                    },
                },
            )
            .catch((err) => {
                console.error('Error in upload image:', err);
            });
    }
}
