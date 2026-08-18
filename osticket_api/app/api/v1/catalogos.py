from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Connection

from app.core.database import obtener_conexion
from app.core.security import PERMISO_LEER, exigir
from app.schemas.ticket import ItemCatalogo
from app.services import ticket_service

# Los ids de tema, estado y prioridad son configurables en cada helpdesk.
# Publicarlos evita que el consumidor los tenga que adivinar o hardcodear.
#
# Acá la autenticación sí va a nivel de router (en tickets.py bajó a cada
# ruta): son tres rutas con el mismo permiso. Y no llevan filtro por
# organización a propósito: temas, estados y prioridades son configuración
# global del helpdesk, no datos de un cliente.
router = APIRouter(
    prefix="/catalogos", tags=["catálogos"],
    dependencies=[Depends(exigir(PERMISO_LEER))],
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
