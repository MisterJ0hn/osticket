from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.engine import Connection

from app.core.database import obtener_conexion
from app.core.security import PERMISO_CREAR, PERMISO_LEER, PERMISO_NOTAS, ClienteApi, exigir
from app.schemas.ticket import (
    CrearTicketRequest,
    CrearTicketResponse,
    EstadoTicketResponse,
    FiltroEstado,
    ListaTicketsResponse,
    ResponderTicketRequest,
    ResponderTicketResponse,
    TicketDetalle,
)
from app.services import ticket_service

# La autenticación ya no va como dependencia del router: cada ruta exige su
# propio permiso (crear o leer), así que la dependencia es distinta según la
# operación. Se sigue cumpliendo lo importante: al declararla como parámetro
# de la función, FastAPI la resuelve ANTES que obtener_conexion, y una
# petición sin clave se rechaza sin llegar a pedirle una conexión al pool.
router = APIRouter(prefix="/tickets", tags=["tickets"])

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
    cliente: ClienteApi = Depends(exigir(PERMISO_CREAR)),
) -> CrearTicketResponse:
    """Crea un ticket a nombre del usuario indicado por `email`.

    Se usa la API nativa de osTicket, que aplica filtros, SLA, auto-respuesta
    y alertas. Si el helpdesk no responde y `OSTICKET_FALLBACK_SQL` está
    activo, el ticket se inserta directamente en la base y la respuesta lo
    indica en `origen`.

    Si el email no existe en osTicket, el usuario se crea automáticamente y
    queda asignado a la organización del cliente que llama, para que después
    pueda leer el ticket que acaba de crear.
    """
    resultado = ticket_service.crear_ticket(
        datos, ip_origen=cliente.ip, org_id=cliente.org_id
    )
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
    cliente: ClienteApi = Depends(exigir(PERMISO_LEER)),
    conexion: Connection = Depends(obtener_conexion),
) ->ListaTicketsResponse:
    """Tickets creados por el usuario, del más nuevo al más antiguo.

    Solo los de la organización del cliente que llama. Un email de otra
    organización responde igual que uno que no existe.
    """
    resultado = ticket_service.listar_tickets(
        conexion,
        email=email,
        org_id=cliente.org_id,
        estado=estado,
        estado_nombre=estado_nombre,
        desde=desde,
        hasta=hasta,
        pagina=pagina,
        tamano=tamano,
    )
    return ListaTicketsResponse(**resultado)


@router.post(
    "/{numero}/mensajes",
    response_model=ResponderTicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Responder un ticket",
)
def responder_ticket(
    datos: ResponderTicketRequest,
    numero: str = Path(..., description="Número del ticket (el que ve el cliente)"),
    cliente: ClienteApi = Depends(exigir(PERMISO_CREAR)),
) -> ResponderTicketResponse:
    """Agrega un mensaje del cliente al hilo de un ticket ya existente.

    `email` tiene que ser el mismo con el que se abrió el ticket. osTicket
    no tiene una API nativa para esto, así que el mensaje (y sus adjuntos)
    se escriben directo en la base: no reabre un ticket cerrado ni avisa al
    agente asignado (ver ticket_write_repo.responder_ticket). Mismo límite
    de tamaño que la creación de tickets (ADJUNTOS_MAX_MB).
    """
    resultado = ticket_service.responder_ticket(
        datos, numero, ip_origen=cliente.ip, org_id=cliente.org_id
    )
    return ResponderTicketResponse(**resultado)


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
                    "para el cliente en el portal, así que solo tiene efecto si "
                    "la clave tiene el permiso 'notas'; sin él se ignora",
    ),
    incluir_contenido: bool = Query(
        False,
        description="Devolver el contenido de los adjuntos en base64, dentro "
                    "de cada mensaje y respuesta. Aumenta mucho el tamaño de "
                    "la respuesta: usar solo cuando se necesiten los archivos",
    ),
    cliente: ClienteApi = Depends(exigir(PERMISO_LEER)),
    conexion: Connection = Depends(obtener_conexion),
) ->TicketDetalle:
    """Cabecera del ticket más el hilo completo de mensajes y respuestas.

    Con `incluir_contenido=true` cada adjunto trae su `contenido_base64`.
    Las imágenes incrustadas en el cuerpo (las de los tickets creados desde
    el portal web) vienen marcadas con `inline: true` y su `cid`, que es lo
    que el HTML del mensaje referencia como `<img src="cid:...">`.
    """
    # Las notas internas son comentarios que los agentes escriben dando por
    # hecho que el cliente no los lee. El filtro por organización no basta
    # acá: son datos de la propia organización, pero igual no le corresponden
    # a un consumidor externo. Se ignora el parámetro en vez de devolver
    # error: quien no tiene el permiso no tiene por qué saber que existe.
    notas = incluir_notas and cliente.puede(PERMISO_NOTAS)
    return TicketDetalle(**ticket_service.obtener_detalle(
        conexion, numero, notas, org_id=cliente.org_id,
        incluir_contenido=incluir_contenido,
    ))


@router.get(
    "/{numero}/estado",
    response_model=EstadoTicketResponse,
    summary="Estado de un ticket",
)
def estado_ticket(
    numero: str = Path(..., description="Número del ticket"),
    cliente: ClienteApi = Depends(exigir(PERMISO_LEER)),
    conexion: Connection = Depends(obtener_conexion),
) ->EstadoTicketResponse:
    """Consulta liviana de estado, pensada para polling.

    `abierto` sale de `ost_ticket_status.state == 'open'` y no del id del
    estado, porque los estados son configurables en osTicket.
    """
    return EstadoTicketResponse(
        **ticket_service.obtener_estado(conexion, numero, org_id=cliente.org_id)
    )
