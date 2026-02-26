import OSS from 'ali-oss';

export class OssService {
    private client: any;
    private static instance: OssService | null = null;

    private constructor(config: any) {
        this.client = new OSS(config);
    }

    static getInstance(): OssService {
        if (!OssService.instance) {
            OssService.instance = new OssService({
                endpoint: process.env.END_POINT,
                accessKeyId: process.env.OSS_ACCESS_KEY_ID!,
                accessKeySecret: process.env.OSS_ACCESS_KEY_SECRET!,
                bucket: process.env.OSS_BUCKET,
                cname: true,
            });
        }
        return OssService.instance;
    }

    async uploadFile(fileName: string, file: any): Promise<any> {
        console.debug('Uploading file to OSS:', fileName);
        return this.client.put(fileName, file);
    }

    async getFile(fileName: string): Promise<any> {
        return this.client.get(fileName);
    }

    async getFileUrl(fileName: string): Promise<string> {
        const options = { expires: 1.5 * 60 * 60 }; // 1.5小时
        return this.client.signatureUrl(fileName, options);
    }
}

export function getOss(): OssService {
    return OssService.getInstance();
}
