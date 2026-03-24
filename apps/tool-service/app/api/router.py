from fastapi import APIRouter

from app.api.donjin import router as donjin_router
from app.api.image import router as image_router
from app.api.image_pipeline import router as image_pipeline_router

api_router = APIRouter()
api_router.include_router(image_router, prefix="/image", tags=["image"])
api_router.include_router(image_pipeline_router, prefix="/image-pipeline", tags=["image-pipeline"])
api_router.include_router(donjin_router, prefix="/sandbox", tags=["sandbox"])
