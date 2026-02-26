import os

from fastapi import FastAPI

from app.api.router import api_router

app = FastAPI(title="tool-service", version=os.getenv("GIT_SHA", "dev"))

app.include_router(api_router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}
