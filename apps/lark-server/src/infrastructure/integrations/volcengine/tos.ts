import { TosClient, TosClientError, TosServerError } from '@volcengine/tos-sdk';

export class TosService {
    private client: TosClient;
    private static instance: TosService;
    private bucket: string;

    constructor() {
        this.bucket = process.env.TOS_BUCKET!;
        this.client = new TosClient({
            accessKeyId: process.env.TOS_ACCESS_KEY_ID!,
            accessKeySecret: process.env.TOS_ACCESS_KEY_SECRET!,
            region: process.env.TOS_REGION!,
            endpoint: process.env.TOS_ENDPOINT!,
        });
    }

    static getInstance(): TosService {
        if (!TosService.instance) {
            TosService.instance = new TosService();
        }
        return TosService.instance;
    }

    async uploadFile(fileName: string, file: Buffer): Promise<any> {
        console.debug('Uploading file to TOS:', fileName);
        try {
            return await this.client.putObject({
                bucket: this.bucket,
                key: fileName,
                body: file,
            });
        } catch (error) {
            this.handleError(error);
            throw error;
        }
    }

    async getFile(fileName: string): Promise<{ content: Buffer }> {
        try {
            const { data } = await this.client.getObjectV2({
                bucket: this.bucket,
                key: fileName,
                dataType: 'buffer',
            });
            return { content: data.content as Buffer };
        } catch (error) {
            this.handleError(error);
            throw error;
        }
    }

    async getFileUrl(fileName: string): Promise<string> {
        try {
            return this.client.getPreSignedUrl({
                bucket: this.bucket,
                key: fileName,
                method: 'GET',
                expires: 1.5 * 60 * 60, // 1.5小时
            });
        } catch (error) {
            this.handleError(error);
            return '';
        }
    }

    private handleError(error: any) {
        if (error instanceof TosClientError) {
            console.error('TOS Client Error:', error.message);
        } else if (error instanceof TosServerError) {
            console.error('TOS Server Error:', {
                requestId: error.requestId,
                statusCode: error.statusCode,
                code: error.code,
                message: error.message,
            });
        } else {
            console.error('Unexpected TOS Error:', error);
        }
    }
}

export function getTos(): TosService {
    return TosService.getInstance();
}

