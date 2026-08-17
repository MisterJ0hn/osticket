from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.engine import Connection

from app.core.database import obtener_conexion
from app.core.security import verificar_acceso
from app.schemas.ticket import (
    CrearTicketRequest,
    CrearTicketResponse,
    EstadoTicketResponse,
    FiltroEstado,
    ListaTicketsResponse,
    TicketDetalle,
)
from app.services import ticket_service

# La autenticación va como dependencia del router, no de cada ruta: las
# dependencias del router se resuelven ANTES que las de la función, así que
# una petición sin clave se rechaza sin llegar a pedirle una conexión al pool.
router = APIRouter(
    prefix="/tickets", tags=["tickets"], dependencies=[Depends(verificar_acceso)]
)

# Las rutas se declaran con `def` y no con `async def` a propósito: por dentro
# todo es I/O bloqueante (SQLAlchemy y httpx síncronos), así que FastAPI las
# ejecuta en su threadpool en vez de trancar el event loop.


@router.post(
    "",
    response_model=CrearTicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear ticket",
)
def crear_ticket(
    datos: CrearTicketRequest,
    # Ya la resolvió el router; FastAPI cachea la dependencia dentro del
    # request, así que acá solo se recoge la IP que devuelve.
    ip_cliente: str = Depends(verificar_acceso),
) -> CrearTicketResponse:
    """Crea un ticket a nombre del usuario indicado por `email`.

    Se usa la API nativa de osTicket, que aplica filtros, SLA, auto-respuesta
    y alertas. Si el helpdesk no responde y `OSTICKET_FALLBACK_SQL` está
    activo, el ticket se inserta directamente en la base y la respuesta lo
    indica en `origen`.

    Si el email no existe en osTicket, el usuario se crea automáticamente.
    """
    resultado = ticket_service.crear_ticket(datos, ip_origen=ip_cliente)
    return CrearTicketResponse(**resultado)


@router.get(
    "",
    response_model=ListaTicketsResponse,
    summary="Obtener tickets de un usuario",
)
def listar_tickets(
    email: str = Query(..., description="Email del usuario dueño de los tickets"),
    estado: FiltroEstado = Query(
        FiltroEstado.ABIERTOS,
        description="Filtro de alto nivel. Por defecto, solo los abiertos",
    ),
    estado_nombre: Optional[str] = Query(
        None,
        description="Nombre exacto de un estado (ver GET /catalogos/estados). "
                    "Si se envía, manda sobre `estado`",
    ),
    desde: Optional[date] = Query(None, description="Creados desde esta fecha (incluida)"),
    hasta: Optional[date] = Query(None, description="Creados hasta esta fecha (incluida)"),
    pagina: int = Query(1, ge=1),
    tamano: int = Query(25, ge=1, le=200),
    conexion: Connection = Depends(obtener_conexion),
) ->ListaTicketsResponse:
    """Tickets creados por el usuario, del más nuevo al más antiguo."""
    resultado = ticket_service.listar_tickets(
        conexion,
        email=email,
        estado=estado,
        estado_nombre=estado_nombre,
        desde=desde,
        hasta=hasta,
        pagina=pagina,
        tamano=tamano,
    )
    return ListaTicketsResponse(**resultado)


@router.get(
    "/{numero}",
    response_model=TicketDetalle,
    summary="Detalle de un ticket",
)
def detalle_ticket(
    numero: str = Path(..., description="Número del ticket (el que ve el cliente)"),
    incluir_notas: bool = Query(
        False,
        description="Incluir las notas internas de los agentes. No son visibles "
                    "para el cliente en el portal: activarlo solo para consumidores internos",
    ),
    conexion: Connection = Depends(obtener_conexion),
) ->TicketDetalle:
    """Cabecera del ticket más el hilo completo de mensajes y respuestas."""
    return TicketDetalle(**ticket_service.obtener_detalle(conexion, numero, incluir_notas))


@router.get(
    "/{numero}/estado",
    response_model=EstadoTicketResponse,
    summary="Estado de un ticket",
)
def estado_ticket(
    numero: str = Path(..., description="Número del ticket"),
    conexion: Connection = Depends(obtener_conexion),
) ->EstadoTicketResponse:
    """Consulta liviana de estado, pensada para polling.

    `abierto` sale de `ost_ticket_status.state == 'open'` y no del id del
    estado, porque los estados son configurables en osTicket.
    """
    return EstadoTicketResponse(**ticket_service.obtener_estado(conexion, numero))
