"""File pipeline HTTP surface — raw-byte sibling of the image pipeline.

``POST /api/file-pipeline/process``: download an inbound Lark file (type=file)
and store the bytes raw to TOS, returning the object-storage reference. Called
fire-and-forget by channel-server when a file message arrives; mirrors the
image-pipeline ``/process`` envelope.
"""
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.middleware.auth import verify_bearer_token
from app.services.attachment_pipeline import process_file_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_bearer_token)])


class ProcessRequest(BaseModel):
    message_id: str | None = None
    file_key: str


@router.post("/process")
async def process(request: ProcessRequest, x_app_name: str = Header(alias="X-App-Name")):
    try:
        result = await process_file_pipeline(
            file_key=request.file_key,
            message_id=request.message_id,
            bot_name=x_app_name,
        )
        return {"success": True, "data": result, "message": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"File process failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
