export interface Meme {
    key: string;
    params_type: {
        min_images?: number;
        max_images?: number;
        min_texts?: number;
        max_texts?: number;
        default_texts?: string[];
    };
    keywords: string[];
}
