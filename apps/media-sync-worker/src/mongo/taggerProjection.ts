import type { Document, Filter, UpdateFilter, UpdateResult } from 'mongodb';
import { extractTaggerSearchTerms } from '../tagger/searchTerms';
import { pixivCollections } from './client';

export interface TaggerProjectionParams {
    pixivAddr: string;
    taskId: string;
    generation: number;
    status: string;
    result: Record<string, unknown>;
}

export interface TaggerProjectionCollection {
    updateMany(
        filter: Filter<Document>,
        update: UpdateFilter<Document>
    ): Promise<Pick<UpdateResult<Document>, 'matchedCount'>>;
}

export async function projectTaggerResultToCollection(
    collection: TaggerProjectionCollection,
    params: TaggerProjectionParams,
    projectedAt = new Date()
): Promise<number> {
    const result = await collection.updateMany(
        {
            pixiv_addr: params.pixivAddr,
            $or: [
                { tagger_generation: { $exists: false } },
                { tagger_generation: { $lt: params.generation } },
                {
                    tagger_generation: params.generation,
                    $or: [
                        { tagger_task_id: { $exists: false } },
                        { tagger_task_id: params.taskId },
                    ],
                },
            ],
        },
        {
            $set: {
                tagger_result: params.result,
                tagger_search_terms: extractTaggerSearchTerms(params.result),
                tagger_task_id: params.taskId,
                tagger_generation: params.generation,
                tagger_status: params.status,
                tagger_updated_at: projectedAt,
            },
        }
    );
    return result.matchedCount;
}

export async function projectTaggerResultToLocal(
    params: TaggerProjectionParams
): Promise<number> {
    const { images } = await pixivCollections();
    return projectTaggerResultToCollection(images.getNativeCollection(), params);
}
