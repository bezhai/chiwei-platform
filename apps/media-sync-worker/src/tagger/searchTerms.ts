export interface TaggerSearchTermLimits {
    maxTerms: number;
    maxLength: number;
    maxDepth: number;
}

const DEFAULT_LIMITS: TaggerSearchTermLimits = {
    maxTerms: 512,
    maxLength: 512,
    maxDepth: 16,
};

export function extractTaggerSearchTerms(
    value: unknown,
    limits: Partial<TaggerSearchTermLimits> = {}
): string[] {
    const resolved = { ...DEFAULT_LIMITS, ...limits };
    const terms = new Set<string>();

    const visit = (current: unknown, depth: number): void => {
        if (terms.size >= resolved.maxTerms || depth > resolved.maxDepth) {
            return;
        }
        if (typeof current === 'string') {
            const term = current.trim().slice(0, resolved.maxLength);
            if (term) terms.add(term);
            return;
        }
        if (Array.isArray(current)) {
            for (const item of current) visit(item, depth + 1);
            return;
        }
        if (typeof current === 'object' && current !== null) {
            for (const item of Object.values(current)) visit(item, depth + 1);
        }
    };

    visit(value, 0);
    return [...terms].slice(0, resolved.maxTerms);
}
