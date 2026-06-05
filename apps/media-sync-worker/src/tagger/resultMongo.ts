import { MongoService, type MongoConfig } from '@inner/shared/mongo';
import { TaggerResultRepository } from './resultRepository';
import type { TaggerImageResultDocument, TaggerTaskDocument } from './types';

export interface TaggerResultMongo {
    service: MongoService;
    repository: TaggerResultRepository;
}

export async function createTaggerResultMongo(config: MongoConfig): Promise<TaggerResultMongo> {
    const service = new MongoService(config);
    await service.initialize();

    const repository = new TaggerResultRepository({
        tasks: service.getCollection<TaggerTaskDocument>('tagger_tasks'),
        imageResults: service.getCollection<TaggerImageResultDocument>('tagger_image_results'),
    });
    await repository.ensureIndexes();

    return { service, repository };
}
