import jieba.analyse
from pydantic import BaseModel


class BatchExtractRequest(BaseModel):
    texts: list[str]
    top_n: int


class ExtractResult(BaseModel):
    text: str
    keywords: list[dict[str, float]]


def extract_batch(request: BatchExtractRequest):
    results = []
    for text in request.texts:
        keywords = jieba.analyse.extract_tags(text, topK=request.top_n, withWeight=True)
        result = {
            "text": text,
            "keywords": [{"word": word, "weight": weight} for word, weight in keywords],
        }
        results.append(result)
    return results
