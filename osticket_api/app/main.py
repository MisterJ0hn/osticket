import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.api_logging import registrar_llamadas_api
from app.core.config import settings
from app.core.database import verificar_conexion, verificar_esquema
from app.core.exceptions import ErrorDominio
from app.core.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Avisa al arrancar de la configuración que dejaría el servicio inútil.

    Se registra como error/warning en vez de abortar: en producción es peor
    que el servicio no levante a que levante quejándose.
    """
    if settings.api_keys:
        logger.warning(
            "API_KEYS tiene %s clave(s) heredadas: son anónimas y ven TODOS "
            "los tickets, sin filtro de organización. Migrarlas a la tabla "
            "api_cliente (scripts/gestionar_clientes.py) y vaciar la variable.",
            len(settings.api_keys),
        )
    if not settings.ips_permitidas:
        logger.warning("IPS_PERMITIDAS está vacío: no se filtra por IP de origen.")
    if not settings.OSTICKET_API_KEY:
        logger.warning(
            "OSTICKET_API_KEY está vacía: la creación por API nativa fallará "
            "y caerá siempre al fallback SQL."
        )
    if not verificar_conexion():
        logger.error(
            "Sin conexión a la base de osTicket (%s@%s:%s): las consultas fallarán.",
            settings.MYSQL_DB, settings.MYSQL_HOST, settings.MYSQL_PORT,
        )
    elif (problema := verificar_esquema()):
        logger.error("Base mal configurada: %s", problema)
    else:
        _avisar_de_clientes()
        _avisar_de_adjuntos()
        _avisar_de_api_key()
        _avisar_de_log_llamadas()
    yield


def _avisar_de_api_key() -> None:
    """Revisa la API Key nativa contra ost_api_key antes del primer ticket.

    Con la clave mal registrada, osTicket contesta 401 y TODOS los tickets
    entran por el fallback SQL: sin adjuntos, sin filtros, sin SLA y sin
    notificaciones. Como el ticket igual se crea, el problema puede pasar
    semanas inadvertido. Detectarlo al arrancar es la diferencia entre
    revisarlo ahora o descubrirlo cuando alguien reclame por una imagen que
    no llegó.
    """
    from app.core import database
    from app.repositories import ticket_repo

    if not settings.OSTICKET_API_KEY:
        return

    try:
        with database.engine.connect() as conexion:
            estado = ticket_repo.estado_api_key(conexion, settings.OSTICKET_API_KEY)
    except Exception:
        logger.exception("No se pudo revisar la API Key de osTicket")
        return

    if estado is None:
        logger.error(
            "La OSTICKET_API_KEY configurada no existe en ost_api_key: osTicket "
            "responderá 401 y todos los tickets caerán al fallback SQL (sin "
            "adjuntos). Darla de alta en Panel Admin -> Manage -> API Keys."
        )
        return

    if not estado["activa"]:
        logger.error("La API Key de osTicket está DESACTIVADA (isactive = 0).")
    if not estado["puede_crear"]:
        logger.error("La API Key de osTicket no tiene permiso para crear tickets.")

    # La IP no se puede comparar desde acá: la que ve osTicket depende del
    # camino de red (con OSTICKET_URL pública, el NAT la reescribe). Se
    # publica para poder cotejarla con la del log de osTicket.
    logger.info(
        "API Key de osTicket registrada para la IP %s. Si osTicket responde "
        "401, es que las peticiones le llegan desde otra IP: comprobar con "
        "docker compose logs osticket | grep api/tickets.json",
        estado["ip_registrada"],
    )


def _avisar_de_adjuntos() -> None:
    """Avisa si osTicket va a descartar los adjuntos sin decir nada.

    Es el peor de los fallos posibles de esta API: el ticket se crea, la
    respuesta dice 201, y las imágenes no llegan a ninguna parte. Sin este
    aviso solo se descubre cuando alguien abre el ticket y no encuentra la
    captura que el usuario juraba haber enviado.
    """
    from app.core import database
    from app.repositories import ticket_repo

    try:
        with database.engine.connect() as conexion:
            habilitados = ticket_repo.adjuntos_habilitados(conexion)
    except Exception:
        logger.exception("No se pudo comprobar si osTicket acepta adjuntos")
        return

    if not habilitados:
        logger.error(
            "osTicket tiene los adjuntos DESHABILITADOS (ost_config."
            "allow_attachments): descartará en silencio los que envíe esta "
            "API y los tickets quedarán sin las imágenes. Activarlo en Panel "
            "Admin -> Settings -> Tickets -> Allow Attachments."
        )


def _avisar_de_log_llamadas() -> None:
    """Avisa si el log de llamados está prendido pero falta la tabla.

    Sin esto, el middleware sigue funcionando (el insert falla en silencio,
    a propósito: ver app/core/api_logging.py) y nadie nota que no se está
    guardando nada hasta que hace falta auditar un llamado.
    """
    from app.core import database
    from app.repositories import log_repo

    if not settings.API_LOG_LLAMADAS:
        return

    try:
        with database.engine.connect() as conexion:
            existe = log_repo.tabla_existe(conexion)
    except Exception:
        logger.exception("No se pudo revisar la tabla api_llamada")
        return

    if not existe:
        logger.error(
            "Falta la tabla api_llamada: correr sql/002_api_llamada.sql. "
            "Mientras tanto el log de llamados a la API no se guarda."
        )


def _avisar_de_clientes() -> None:
    """Distingue los dos motivos por los que nadie podría autenticarse.

    Sin esto, tanto olvidar el DDL como no haber dado de alta a nadie se
    manifiestan igual: un 403 en todas las peticiones, que es un síntoma que
    no apunta a ninguna de las dos causas.
    """
    from app.core import database
    from app.repositories import cliente_repo

    try:
        with database.engine.connect() as conexion:
            if not cliente_repo.tabla_existe(conexion):
                logger.error(
                    "Falta la tabla api_cliente: correr sql/001_api_cliente.sql. "
                    "Sin ella no hay más claves que las heredadas de API_KEYS."
                )
                return
            activos = len(cliente_repo.listar_activos(conexion))
    except Exception:
        logger.exception("No se pudo revisar la tabla api_cliente")
        return

    if not activos and not settings.api_keys:
        logger.error(
            "No hay ningún cliente activo en api_cliente ni claves en "
            "API_KEYS: la API rechazará todas las peticiones."
        )
    else:
        logger.info("Clientes activos en api_cliente: %s (la caché se refresca "
                    "cada %ss)", activos, settings.CACHE_CLIENTES_TTL)


app = FastAPI(
    lifespan=lifespan,
    title="osTicket API",
    description=(
        "Fachada REST sobre osTicket v1.18 para Alfaro Madariaga.\n\n"
        "osTicket solo expone una API nativa de **creación** de tickets; las "
        "consultas de este servicio se resuelven leyendo su base MySQL.\n\n"
        "Todos los endpoints exigen el header `X-API-Key` y que la IP de "
        "origen esté en la allowlist."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(registrar_llamadas_api)


@app.exception_handler(ErrorDominio)
async def manejar_error_dominio(request: Request, exc: ErrorDominio):
    """Los errores esperables salen con la misma forma que el resto."""
    return JSONResponse(
        status_code=exc.http_status,
        content={"exito": False, "mensaje": exc.mensaje},
    )


@app.exception_handler(HTTPException)
async def manejar_http_exception(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"exito": False, "mensaje": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def manejar_error_no_controlado(request: Request, exc: Exception):
    # El detalle va al log, no a la respuesta: puede llevar fragmentos de SQL
    # o credenciales de la conexión.
    logger.exception("Error no controlado en %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"exito": False, "mensaje": "Error interno del servidor"},
    )


app.include_router(api_router)


def _estado_de_api_key() -> str:
    """Estado de la API Key nativa, para verlo sin entrar al servidor."""
    from app.core import database
    from app.repositories import ticket_repo

    if not settings.OSTICKET_API_KEY:
        return "sin OSTICKET_API_KEY: todo entra por el fallback SQL"

    try:
        with database.engine.connect() as conexion:
            estado = ticket_repo.estado_api_key(conexion, settings.OSTICKET_API_KEY)
    except Exception:
        return "no se pudo comprobar"

    if estado is None:
        return "la clave configurada NO existe en ost_api_key"
    if not estado["activa"]:
        return "la clave está desactivada (isactive = 0)"
    if not estado["puede_crear"]:
        return "la clave no puede crear tickets"
    return f"clave ok, registrada para la IP {estado['ip_registrada']}"


def _estado_de_adjuntos() -> str:
    from app.core import database
    from app.repositories import ticket_repo

    try:
        with database.engine.connect() as conexion:
            return "ok" if ticket_repo.adjuntos_habilitados(conexion) else (
                "DESHABILITADOS en osTicket: los descartará en silencio"
            )
    except Exception:
        return "no se pudo comprobar"


@app.get("/salud", tags=["servicio"], summary="Estado del servicio")
def salud():
    """Sin autenticación: lo consulta el monitor/orquestador."""
    base_ok = verificar_conexion()
    problema = verificar_esquema() if base_ok else None
    # Se publica acá porque es un fallo que no se ve de ninguna otra forma:
    # con los adjuntos deshabilitados osTicket los descarta sin error y los
    # tickets quedan sin las imágenes.
    adjuntos = _estado_de_adjuntos() if base_ok else "desconocido"
    api_nativa = _estado_de_api_key() if base_ok else "desconocido"

    return {
        "exito": base_ok and not problema,
        "base_datos": "ok" if base_ok else "sin conexión",
        "adjuntos": adjuntos,
        "api_nativa": api_nativa,
        # Distingue "no conecto" de "conecto a la base equivocada", que es el
        # fallo silencioso: la conexión funciona pero los datos no son los del
        # helpdesk.
        "esquema": problema or "ok",
        "conexion": f"{settings.MYSQL_USER}@{settings.MYSQL_HOST}:"
                    f"{settings.MYSQL_PORT}/{settings.MYSQL_DB}",
        "osticket_url": settings.osticket_url_base,
        "fallback_sql": settings.OSTICKET_FALLBACK_SQL,
    }
