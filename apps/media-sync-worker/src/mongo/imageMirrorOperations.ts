import type { AnyBulkWriteOperation, Document } from 'mongodb';

const LOCAL_OWNED_FIELDS = new Set([
    'image_key',
    'width',
    'height',
    'update_time',
    'tagger_result',
    'tagger_search_terms',
    'tagger_task_id',
    'tagger_generation',
    'tagger_status',
    'tagger_updated_at',
]);

export function buildPixivImageMirrorOperations(
    docs: Document[]
): AnyBulkWriteOperation<Document>[] {
    return docs.map((doc) => {
        const { _id, ...sourceFields } = doc;
        const mutableSourceFields: Document = {};
        const insertOnlyFields: Document = { _id };
        for (const [field, value] of Object.entries(sourceFields)) {
            if (LOCAL_OWNED_FIELDS.has(field)) {
                insertOnlyFields[field] = value;
            } else {
                mutableSourceFields[field] = value;
            }
        }
        return {
            updateOne: {
                filter: { _id },
                update: {
                    $set: mutableSourceFields,
                    $setOnInsert: insertOnlyFields,
                },
                upsert: true,
            },
        };
    });
}
