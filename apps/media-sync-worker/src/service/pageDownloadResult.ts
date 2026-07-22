export type PageDownloadStatus =
    | 'downloaded'
    | 'exists'
    | 'missing_url'
    | 'download_failed'
    | 'add_image_failed'
    | 'post_sync_failed';

export interface PageDownloadOutcome {
    status: PageDownloadStatus;
    error?: string;
}

const SUCCESS_STATUSES = new Set<PageDownloadStatus>(['downloaded', 'exists']);

export function assertPageDownloadsSucceeded(
    illustId: string,
    outcomes: PageDownloadOutcome[]
): void {
    const failures = outcomes.flatMap((outcome, index) => {
        if (SUCCESS_STATUSES.has(outcome.status)) {
            return [];
        }
        const detail = outcome.error ? `: ${outcome.error}` : '';
        return [`page ${index + 1} ${outcome.status}${detail}`];
    });

    if (failures.length > 0) {
        throw new Error(`illustration ${illustId} incomplete: ${failures.join('; ')}`);
    }
}
