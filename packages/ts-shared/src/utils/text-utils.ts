/**
 * Text utility functions
 */
export class TextUtils {
    /**
     * Clear text by removing user mentions and normalizing whitespace
     */
    static clearText(text: string): string {
        return text
            .replace(/@_user_\d+/g, '')
            .replace(/\s+/g, ' ')
            .trim();
    }

    /**
     * Remove emoji-like patterns from text
     */
    static removeEmoji(text: string): string {
        return text.replace(/\[[^\]]+\]/g, '').replace(/<[^<>]+>/g, '');
    }

    /**
     * Truncate text to a maximum length with ellipsis
     */
    static truncate(text: string, maxLength: number, ellipsis = '...'): string {
        if (text.length <= maxLength) {
            return text;
        }
        return text.slice(0, maxLength - ellipsis.length) + ellipsis;
    }

    /**
     * Escape HTML special characters
     */
    static escapeHtml(text: string): string {
        const htmlEntities: Record<string, string> = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        };
        return text.replace(/[&<>"']/g, (char) => htmlEntities[char]);
    }

    /**
     * Check if string is empty or whitespace only
     */
    static isBlank(text: string | null | undefined): boolean {
        return !text || text.trim().length === 0;
    }
}
