from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Connection

from app.core.database import obtener_conexion
from app.core.security import verificar_acceso
from app.schemas.ticket import ItemCatalogo
from app.services import ticket_service

# Los ids de tema, estado y prioridad son configurables en cada helpdesk.
# Publicarlos evita que el consumidor los tenga que adivinar o hardcodear.
# La autenticación va a nivel de router para que se resuelva antes de pedir
# conexión al pool (ver el comentario equivalente en tickets.py).
router = APIRouter(
    prefix="/catalogos", tags=["catálogos"], dependencies=[Depends(verificar_acceso)]
)


@router.get("/temas", response_model=List[ItemCatalogo], summary="Temas de ayuda")
def temas(conexion: Connection = Depends(obtener_conexion)) -> List[ItemCatalogo]:
    return [ItemCatalogo(**item) for item in ticket_service.catalogo(conexion, "temas")]


@router.get("/estados", response_model=List[ItemCatalogo], summary="Estados de ticket")
def estados(conexion: Connection = Depends(obtener_conexion)) -> List[ItemCatalogo]:
    return [ItemCatalogo(**item) for item in ticket_service.catalogo(conexion, "estados")]


@router.get("/prioridades", response_model=List[ItemCatalogo], summary="Prioridades")
def prioridades(conexion: Connection = Depends(obtener_conexion)) -> List[ItemCatalogo]:
    return [ItemCatalogo(**item) for item in ticket_service.catalogo(conexion, "prioridades")]
