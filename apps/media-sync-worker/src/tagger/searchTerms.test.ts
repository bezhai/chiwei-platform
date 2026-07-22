import { describe, expect, it } from 'bun:test';
import { extractTaggerSearchTerms } from './searchTerms';

describe('extractTaggerSearchTerms', () => {
    it('indexes string leaves from the complete dynamic row without field whitelisting', () => {
        const row = {
            id: 'a.jpg',
            wd14: { tags: [{ tag: '1girl', category: 'general' }] },
            describe_a: { main_subject: 'girl under cherry blossoms' },
            ocr: { ocr_text: '春日' },
            future_capability: { nested: ['kept', { value: 'also kept' }] },
        };

        expect(extractTaggerSearchTerms(row)).toEqual([
            'a.jpg',
            '1girl',
            'general',
            'girl under cherry blossoms',
            '春日',
            'kept',
            'also kept',
        ]);
    });

    it('deduplicates, trims and applies limits only to the derived index', () => {
        const long = 'x'.repeat(600);
        const row = { first: ' solo ', second: ['solo', long], raw: { untouched: long } };

        const terms = extractTaggerSearchTerms(row, { maxTerms: 2, maxLength: 512, maxDepth: 16 });

        expect(terms).toEqual(['solo', 'x'.repeat(512)]);
        expect(row.raw.untouched).toHaveLength(600);
    });

    it('stops traversing beyond the configured depth', () => {
        expect(extractTaggerSearchTerms({ a: { b: { c: 'too deep' } } }, { maxDepth: 1 })).toEqual([]);
    });
});
