from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.services.extraction_service import BatchExtractRequest, extract_batch

router = APIRouter()


@router.post("/extract_batch")
async def extract_batch_api(request: BatchExtractRequest):
    try:
        entities = extract_batch(request)
        return JSONResponse(content=entities)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": "Internal Server Error", "details": str(e)},
        )
