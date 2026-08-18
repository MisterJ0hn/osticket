import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
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
    yield


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


@app.get("/salud", tags=["servicio"], summary="Estado del servicio")
def salud():
    """Sin autenticación: lo consulta el monitor/orquestador."""
    base_ok = verificar_conexion()
    problema = verificar_esquema() if base_ok else None
    return {
        "exito": base_ok and not problema,
        "base_datos": "ok" if base_ok else "sin conexión",
        # Distingue "no conecto" de "conecto a la base equivocada", que es el
        # fallo silencioso: la conexión funciona pero los datos no son los del
        # helpdesk.
        "esquema": problema or "ok",
        "conexion": f"{settings.MYSQL_USER}@{settings.MYSQL_HOST}:"
                    f"{settings.MYSQL_PORT}/{settings.MYSQL_DB}",
        "osticket_url": settings.osticket_url_base,
        "fallback_sql": settings.OSTICKET_FALLBACK_SQL,
    }
