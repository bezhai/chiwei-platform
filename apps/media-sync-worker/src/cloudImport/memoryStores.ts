/**
 * cloud import 测试用的内存替身：一个 Mongo 集合和一个 Redis。
 *
 * 只实现导入用到的那部分 Mongo 语义，但实现的部分按真实行为来：
 * - 查询：字段相等、$in、$gt；$gt 按 BSON 类型分组比较（类型不同不匹配），排序按 BSON 类型顺序；
 * - 更新：只接受 $set / $setOnInsert，两个操作符写同一个字段会报冲突；upsert 时用 filter 的相等条件
 *   加 $setOnInsert 和 $set 生成新文档；_id 重复抛 11000；
 * - 文档经 BSON 序列化往返后存储，和 driver 写库一样会丢掉 undefined 字段。
 * 没有建模的操作符、选项和类型一律抛错，不悄悄放过。
 */
import { BSON, ObjectId, type Document } from 'mongodb';

const { EJSON } = BSON;

function unsupported(what: string): Error {
    return new Error(`memoryStores: ${what} is not modeled`);
}

function cloneDoc<T extends Document>(doc: T): T {
    return BSON.deserialize(BSON.serialize(doc)) as T;
}

function cloneValue(value: unknown): unknown {
    return cloneDoc({ v: value }).v;
}

function valueKey(value: unknown): string {
    return EJSON.stringify({ v: value }, { relaxed: false });
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        return false;
    }
    const proto = Object.getPrototypeOf(value);
    return proto === Object.prototype || proto === null;
}

function isOperatorObject(value: unknown): value is Record<string, unknown> {
    if (!isPlainObject(value)) {
        return false;
    }
    const keys = Object.keys(value);
    const operatorKeys = keys.filter((key) => key.startsWith('$'));
    if (operatorKeys.length > 0 && operatorKeys.length !== keys.length) {
        throw new Error(`memoryStores: cannot mix operators and fields in ${JSON.stringify(keys)}`);
    }
    return operatorKeys.length > 0;
}

// BSON 比较顺序：MinKey < Null < Numbers < String < Object < Array < BinData < ObjectId < Boolean < Date
function typeRank(value: unknown): number {
    if (value === undefined || value === null) return 1;
    if (typeof value === 'number') return 2;
    if (typeof value === 'string') return 3;
    if (value instanceof ObjectId) return 8;
    if (typeof value === 'boolean') return 9;
    if (value instanceof Date) return 10;
    throw unsupported(`ordering of ${Object.prototype.toString.call(value)}`);
}

function compareBson(a: unknown, b: unknown): number {
    const rankA = typeRank(a);
    const rankB = typeRank(b);
    if (rankA !== rankB) {
        return rankA - rankB;
    }
    switch (rankA) {
        case 1:
            return 0;
        case 2:
            return (a as number) - (b as number);
        case 3:
            return a === b ? 0 : (a as string) < (b as string) ? -1 : 1;
        case 8: {
            const hexA = (a as ObjectId).toHexString();
            const hexB = (b as ObjectId).toHexString();
            return hexA === hexB ? 0 : hexA < hexB ? -1 : 1;
        }
        case 9:
            return Number(a) - Number(b);
        default:
            return (a as Date).getTime() - (b as Date).getTime();
    }
}

function equalsValue(fieldValue: unknown, expected: unknown): boolean {
    if (expected === undefined) {
        throw unsupported('matching against undefined');
    }
    if (Array.isArray(fieldValue)) {
        throw unsupported('matching array-valued fields');
    }
    if (expected === null) {
        return fieldValue === undefined || fieldValue === null;
    }
    return valueKey(fieldValue) === valueKey(expected);
}

function assertTopLevelField(field: string): void {
    if (field.startsWith('$') || field.includes('.')) {
        throw unsupported(`field path ${field}`);
    }
}

function matches(doc: Document, filter: Document): boolean {
    for (const [field, condition] of Object.entries(filter)) {
        assertTopLevelField(field);
        const fieldValue = doc[field];
        if (!isOperatorObject(condition)) {
            if (!equalsValue(fieldValue, condition)) {
                return false;
            }
            continue;
        }
        for (const [operator, argument] of Object.entries(condition)) {
            switch (operator) {
                case '$in':
                    if (!Array.isArray(argument)) {
                        throw new Error('memoryStores: $in needs an array');
                    }
                    if (!argument.some((candidate) => equalsValue(fieldValue, candidate))) {
                        return false;
                    }
                    break;
                case '$gt':
                    if (argument === undefined || argument === null) {
                        throw unsupported('$gt against null');
                    }
                    if (Array.isArray(fieldValue)) {
                        throw unsupported('$gt on array-valued fields');
                    }
                    // 类型分组：$gt 只匹配与参数同一 BSON 类型的值，缺失字段不匹配
                    if (fieldValue === undefined || typeRank(fieldValue) !== typeRank(argument)) {
                        return false;
                    }
                    if (compareBson(fieldValue, argument) <= 0) {
                        return false;
                    }
                    break;
                default:
                    throw unsupported(`query operator ${operator}`);
            }
        }
    }
    return true;
}

function assertKnownKeys(value: object, allowed: string[], context: string): void {
    for (const key of Object.keys(value)) {
        if (!allowed.includes(key)) {
            throw unsupported(`${context} option ${key}`);
        }
    }
}

export interface FindOptionsModel {
    sort?: Document;
    limit?: number;
    projection?: Document;
}

export interface BulkCounts {
    insertedCount: number;
    matchedCount: number;
    modifiedCount: number;
    deletedCount: number;
    upsertedCount: number;
}

export class DuplicateKeyError extends Error {
    readonly code = 11000;
    constructor(message: string, readonly writeErrors: Array<{ index: number; message: string }> = []) {
        super(message);
    }
}

export class MemoryCollection {
    private docs: Document[];
    /** 按调用顺序记录的方法名，用来断言某个集合只被追加写入 */
    readonly calls: string[] = [];

    constructor(readonly collectionName: string, initial: Document[] = []) {
        this.docs = [];
        for (const doc of initial) {
            this.insertNow({ _id: new ObjectId(), ...doc });
        }
    }

    /** 当前全部文档的副本，按插入顺序 */
    snapshot(): Document[] {
        return this.docs.map((doc) => cloneDoc(doc));
    }

    find(filter: Document, options: FindOptionsModel = {}): { toArray(): Promise<Document[]> } {
        this.calls.push('find');
        return {
            toArray: async () => this.evaluateFind(filter, options),
        };
    }

    async countDocuments(filter: Document, ...rest: unknown[]): Promise<number> {
        this.calls.push('countDocuments');
        if (rest.length > 0) {
            throw unsupported('countDocuments options');
        }
        return this.docs.filter((doc) => matches(doc, filter)).length;
    }

    async insertOne(doc: Document, ...rest: unknown[]): Promise<{ insertedId: unknown }> {
        this.calls.push('insertOne');
        if (rest.length > 0) {
            throw unsupported('insertOne options');
        }
        // driver 在客户端补 _id 并写回传入的对象
        if (doc._id === undefined) {
            doc._id = new ObjectId();
        }
        this.insertNow(doc);
        return { insertedId: doc._id };
    }

    async bulkWrite(operations: Document[], options: { ordered?: boolean } = {}): Promise<BulkCounts> {
        this.calls.push('bulkWrite');
        assertKnownKeys(options, ['ordered'], 'bulkWrite');
        const ordered = options.ordered ?? true;
        const counts: BulkCounts = {
            insertedCount: 0,
            matchedCount: 0,
            modifiedCount: 0,
            deletedCount: 0,
            upsertedCount: 0,
        };
        const writeErrors: Array<{ index: number; message: string }> = [];
        for (const [index, operation] of operations.entries()) {
            const kinds = Object.keys(operation);
            if (kinds.length !== 1 || kinds[0] !== 'updateOne') {
                throw unsupported(`bulk operation ${kinds.join(',')}`);
            }
            try {
                this.applyUpdateOne(operation.updateOne, counts);
            } catch (err) {
                if (!(err instanceof DuplicateKeyError)) {
                    throw err;
                }
                writeErrors.push({ index, message: err.message });
                if (ordered) {
                    break;
                }
            }
        }
        if (writeErrors.length > 0) {
            throw new DuplicateKeyError(`E11000 duplicate key error in ${this.collectionName}`, writeErrors);
        }
        return counts;
    }

    private evaluateFind(filter: Document, options: FindOptionsModel): Document[] {
        assertKnownKeys(options, ['sort', 'limit', 'projection'], 'find');
        let result = this.docs.filter((doc) => matches(doc, filter));

        if (options.sort) {
            const keys = Object.entries(options.sort);
            for (const [field, direction] of keys) {
                assertTopLevelField(field);
                if (direction !== 1 && direction !== -1) {
                    throw unsupported(`sort direction ${String(direction)}`);
                }
            }
            result = [...result].sort((a, b) => {
                for (const [field, direction] of keys) {
                    const order = compareBson(a[field], b[field]);
                    if (order !== 0) {
                        return order * (direction as number);
                    }
                }
                return 0;
            });
        }

        if (options.limit !== undefined) {
            if (!Number.isInteger(options.limit) || options.limit < 0) {
                throw new Error(`memoryStores: invalid limit ${options.limit}`);
            }
            if (options.limit > 0) {
                result = result.slice(0, options.limit);
            }
        }

        const projection = options.projection;
        if (projection) {
            for (const [field, include] of Object.entries(projection)) {
                assertTopLevelField(field);
                if (include !== 1 && include !== true) {
                    throw unsupported(`projection ${field}: ${String(include)}`);
                }
            }
            result = result.map((doc) => {
                const projected: Document = { _id: doc._id };
                for (const field of Object.keys(projection)) {
                    if (doc[field] !== undefined) {
                        projected[field] = doc[field];
                    }
                }
                return projected;
            });
        }

        return result.map((doc) => cloneDoc(doc));
    }

    private applyUpdateOne(spec: Document, counts: BulkCounts): void {
        assertKnownKeys(spec, ['filter', 'update', 'upsert'], 'updateOne');
        const { filter, update, upsert } = spec;
        if (!isPlainObject(update)) {
            throw unsupported('pipeline or non-object updates');
        }
        const operators = Object.keys(update);
        if (operators.length === 0 || !operators.every((key) => key.startsWith('$'))) {
            throw new Error('memoryStores: updateOne requires atomic operators');
        }
        for (const operator of operators) {
            if (operator !== '$set' && operator !== '$setOnInsert') {
                throw unsupported(`update operator ${operator}`);
            }
        }
        const set: Document = update.$set ?? {};
        const setOnInsert: Document = update.$setOnInsert ?? {};
        for (const field of [...Object.keys(set), ...Object.keys(setOnInsert)]) {
            assertTopLevelField(field);
        }
        for (const field of Object.keys(set)) {
            if (field in setOnInsert) {
                throw new Error(`memoryStores: Updating the path '${field}' would create a conflict at '${field}'`);
            }
        }
        if ('_id' in set) {
            throw unsupported('$set on _id');
        }

        const existing = this.docs.find((doc) => matches(doc, filter));
        if (existing) {
            counts.matchedCount += 1;
            let changed = false;
            for (const [field, value] of Object.entries(set)) {
                if (valueKey(existing[field]) !== valueKey(value)) {
                    existing[field] = cloneValue(value);
                    changed = true;
                }
            }
            if (changed) {
                counts.modifiedCount += 1;
            }
            return;
        }
        if (upsert !== true) {
            return;
        }

        const inserted: Document = {};
        for (const [field, condition] of Object.entries(filter as Document)) {
            if (!isOperatorObject(condition)) {
                inserted[field] = condition;
            }
        }
        Object.assign(inserted, setOnInsert, set);
        if (inserted._id === undefined) {
            inserted._id = new ObjectId();
        }
        this.insertNow(inserted);
        counts.upsertedCount += 1;
    }

    private insertNow(doc: Document): void {
        if (this.docs.some((existing) => valueKey(existing._id) === valueKey(doc._id))) {
            throw new DuplicateKeyError(
                `E11000 duplicate key error collection: ${this.collectionName} dup key: { _id: ${valueKey(doc._id)} }`
            );
        }
        this.docs.push(cloneDoc(doc));
    }
}

export interface MemoryRedisData {
    hashes?: Record<string, Record<string, string>>;
    sets?: Record<string, string[]>;
    strings?: Record<string, string>;
}

/** 只实现导入用到的三个读命令；key 类型不对时和真实 Redis 一样报 WRONGTYPE。 */
export class MemoryRedis {
    constructor(private readonly data: MemoryRedisData = {}) {}

    async hgetall(key: string): Promise<Record<string, string>> {
        this.assertType(key, 'hashes');
        return { ...(this.data.hashes?.[key] ?? {}) };
    }

    async smembers(key: string): Promise<string[]> {
        this.assertType(key, 'sets');
        return [...new Set(this.data.sets?.[key] ?? [])];
    }

    async get(key: string): Promise<string | null> {
        this.assertType(key, 'strings');
        return this.data.strings?.[key] ?? null;
    }

    private assertType(key: string, expected: keyof MemoryRedisData): void {
        for (const kind of ['hashes', 'sets', 'strings'] as const) {
            if (kind !== expected && this.data[kind] && key in this.data[kind]!) {
                throw new Error('WRONGTYPE Operation against a key holding the wrong kind of value');
            }
        }
    }
}
