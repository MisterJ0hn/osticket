from fastapi import APIRouter

from app.api.v1 import catalogos, tickets

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(tickets.router)
api_router.include_router(catalogos.router)
