import { MongoClient, Document } from 'mongodb';

let mongoClient: MongoClient | null = null;

const DANGEROUS_OPERATORS = new Set([
  '$where',
  '$expr',
  '$function',
  '$accumulator',
]);

const containsDangerousOperator = (value: unknown): boolean => {
  if (!value || typeof value !== 'object') {
    return false;
  }
  if (Array.isArray(value)) {
    return value.some((item) => containsDangerousOperator(item));
  }
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    if (DANGEROUS_OPERATORS.has(key)) {
      return true;
    }
    if (containsDangerousOperator(child)) {
      return true;
    }
  }
  return false;
};

const buildMongoUrl = () => {
  const username = process.env.MONGO_INITDB_ROOT_USERNAME || '';
  const password = process.env.MONGO_INITDB_ROOT_PASSWORD || '';
  const host = process.env.MONGO_HOST || 'localhost';

  return (
    `mongodb://${username}:${password}@${host}/chiwei?` +
    `connectTimeoutMS=2000&authSource=admin`
  );
};

export const initMongo = async () => {
  if (mongoClient) {
    return mongoClient;
  }
  const url = buildMongoUrl();
  mongoClient = new MongoClient(url);
  await mongoClient.connect();
  return mongoClient;
};

export const queryLarkEvents = async (options: {
  filter?: Document;
  projection?: Document;
  sort?: Document;
  skip?: number;
  limit?: number;
}) => {
  const client = await initMongo();
  const db = client.db('chiwei');
  const collection = db.collection('lark_event');

  const filter = options.filter ?? {};
  const projection = options.projection ?? {};
  const sort = options.sort ?? { created_at: -1 };
  const skip = options.skip ?? 0;
  const limit = options.limit ?? 20;

  if (
    containsDangerousOperator(filter) ||
    containsDangerousOperator(projection) ||
    containsDangerousOperator(sort)
  ) {
    throw new Error('Dangerous MongoDB operator detected');
  }

  const cursor = collection.find(filter, { projection }).sort(sort).skip(skip).limit(limit);
  const data = await cursor.toArray();
  const total = await collection.countDocuments(filter);
  return { data, total };
};
