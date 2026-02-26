from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import Response

from app.services.image_service import process_image

router = APIRouter()


@router.post("/process")
async def process_image_endpoint(
    file: UploadFile = File(...),
    max_width: int | None = Query(None, gt=0),
    max_height: int | None = Query(None, gt=0),
    quality: int = Query(85, ge=1, le=100),
    format: str | None = Query(None),
):
    data = await file.read()
    original_size = len(data)

    output_bytes, width, height = process_image(
        data=data,
        max_width=max_width,
        max_height=max_height,
        quality=quality,
        format=format,
    )

    # 根据输出格式确定 content type
    fmt = (format or "JPEG").upper()
    media_types = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }
    media_type = media_types.get(fmt, "application/octet-stream")

    return Response(
        content=output_bytes,
        media_type=media_type,
        headers={
            "X-Image-Width": str(width),
            "X-Image-Height": str(height),
            "X-Original-Size": str(original_size),
            "X-Output-Size": str(len(output_bytes)),
        },
    )
