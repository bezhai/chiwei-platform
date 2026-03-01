import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.middleware.auth import verify_bearer_token
from app.services.image_pipeline import process_image_pipeline, upload_base64_image

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_bearer_token)])


class ProcessRequest(BaseModel):
    message_id: str | None = None
    file_key: str


class UploadBase64Request(BaseModel):
    base64_data: str


@router.post("/process")
async def process(request: ProcessRequest, x_app_name: str = Header(alias="X-App-Name")):
    try:
        result = await process_image_pipeline(
            file_key=request.file_key,
            message_id=request.message_id,
            bot_name=x_app_name,
        )
        return {"success": True, "data": result, "message": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Image process failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload-base64")
async def upload_base64(request: UploadBase64Request, x_app_name: str = Header(alias="X-App-Name")):
    try:
        result = await upload_base64_image(
            base64_data=request.base64_data,
            bot_name=x_app_name,
        )
        return {"success": True, "data": result, "message": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Base64 upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
