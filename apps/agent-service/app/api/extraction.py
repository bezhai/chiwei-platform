"""
文本提取相关API路由
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.utils import BatchExtractRequest, extract_batch

router = APIRouter()


@router.post("/extract_batch")
async def extract_batch_api(request: BatchExtractRequest):
    """
    批量提取文本中的实体。
    :param request: 请求体，包含模型、文本列表等参数
    :return: 提取的实体列表
    """
    try:
        entities = extract_batch(request)
        return JSONResponse(content=entities)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": "Internal Server Error", "details": str(e)},
        )
