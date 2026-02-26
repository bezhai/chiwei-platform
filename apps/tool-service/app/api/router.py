from fastapi import APIRouter

from app.api.image import router as image_router

api_router = APIRouter()
api_router.include_router(image_router, prefix="/image", tags=["image"])
