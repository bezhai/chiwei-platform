import { Collection, MongoClient, Document } from 'mongodb';

class MongoService {
    private static instance: MongoService;
    private client: MongoClient | null = null;
    private larkEventCollection: Collection | null = null;

    private constructor() {}

    public static getInstance(): MongoService {
        if (!MongoService.instance) {
            MongoService.instance = new MongoService();
        }
        return MongoService.instance;
    }

    public async initialize() {
        try {
            const {
                MONGO_INITDB_ROOT_USERNAME: username,
                MONGO_INITDB_ROOT_PASSWORD: password,
                MONGO_HOST: host,
            } = process.env;

            const url =
                `mongodb://${username}:${password}@${host}/chiwei?` +
                `connectTimeoutMS=2000&` +
                `authSource=admin`;

            this.client = new MongoClient(url);
            await this.client.connect();

            const database = this.client.db('chiwei');

            this.larkEventCollection = database.collection('lark_event');

            console.info('MongoDB initialization completed.');
        } catch (err) {
            console.error('MongoDB initialization failed:', err);
            throw err;
        }
    }

    public async insertLarkEvent(event: Document) {
        if (!this.larkEventCollection) {
            throw new Error('MongoDB not initialized');
        }
        return this.larkEventCollection.insertOne(event);
    }
}

// 导出单例实例
const mongoService = MongoService.getInstance();

// 导出初始化函数
export const mongoInitPromise = async () => {
    await mongoService.initialize();
};

export const insertEvent = async (event: Document) => {
    await mongoService.insertLarkEvent(event);
};
