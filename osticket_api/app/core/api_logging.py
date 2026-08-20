"""Middleware que deja constancia de cada llamado a esta API en `api_llamada`.

Registra IP, método+ruta, request, response y fecha. El contenido base64 de
los adjuntos (ver `app/schemas/ticket.py`) se redacta antes de guardarse: es
lo único de un payload típico que puede pesar varios MB, y no aporta nada
para auditar quién llamó y con qué.
"""

import json
import logging
from typing import Any, Optional

from fastapi import Request, Response
from starlette.concurrency import run_in_threadpool

from app.core import database
from app.core.config import settings
from app.core.security import ip_del_cliente
from app.repositories import log_repo

logger = logging.getLogger(__name__)

_CLAVE_BASE64 = "contenido_base64"


def _redactar(valor: Any) -> Any:
    """Reemplaza cualquier `contenido_base64` por un placeholder con su tamaño.

    Recorre dicts/listas anidados porque tanto el request de creación
    (`Adjunto`) como el response de detalle (`AdjuntoInfo`) pueden traer
    varios adjuntos, cada uno con su propio campo.
    """
    if isinstance(valor, dict):
        redactado = {}
        for clave, sub in valor.items():
            if clave == _CLAVE_BASE64 and isinstance(sub, str):
                redactado[clave] = f"<base64 omitido, {len(sub)} caracteres>"
            else:
                redactado[clave] = _redactar(sub)
        return redactado
    if isinstance(valor, list):
        return [_redactar(item) for item in valor]
    return valor


def _resumir_cuerpo(cuerpo: bytes) -> Optional[str]:
    """JSON redactado y compacto, o texto plano si no es JSON; None si vacío."""
    if not cuerpo:
        return None

    try:
        datos = json.loads(cuerpo)
    except (json.JSONDecodeError, UnicodeDecodeError):
        texto = cuerpo.decode("utf-8", errors="replace")
    else:
        texto = json.dumps(_redactar(datos), ensure_ascii=False, separators=(",", ":"))

    maximo = settings.API_LOG_BODY_MAX_CHARS
    if len(texto) > maximo:
        texto = f"{texto[:maximo]}... [truncado, {len(texto)} caracteres en total]"
    return texto


def _guardar_log(
    *, ip: str, metodo: str,
    request_txt: Optional[str],
    response_code: int,
    response_txt: Optional[str],
) -> None:
    """Corre en el threadpool: la conexión de SQLAlchemy acá es síncrona.

    Si falla (tabla faltante, base caída, lo que sea) solo se deja un
    warning: un fallo de auditoría no puede tumbar una petición que por lo
    demás es válida (mismo criterio que `_registrar_uso` en security.py).
    """
    try:
        with database.transaccion() as conexion:
            log_repo.registrar(
                conexion,
                ip=ip,
                metodo=metodo,
                request=request_txt,
                response_code=response_code,
                response=response_txt,
            )
    except Exception:
        logger.warning("No se pudo registrar el llamado a %s", metodo, exc_info=True)


async def registrar_llamadas_api(request: Request, call_next):
    """Middleware HTTP: solo actúa sobre `/api/*` (deja fuera /salud, /docs...)."""
    if not settings.API_LOG_LLAMADAS or not request.url.path.startswith("/api/"):
        return await call_next(request)

    cuerpo_pedido = await request.body()

    # FastAPI/Pydantic todavía necesitan leer el body para validar la
    # petición: como ya se consumió acá, se reemplaza el receive por uno
    # que reproduce los mismos bytes.
    async def receive():
        return {"type": "http.request", "body": cuerpo_pedido, "more_body": False}

    request._receive = receive  # type: ignore[attr-defined]

    if cuerpo_pedido:
        texto_pedido = _resumir_cuerpo(cuerpo_pedido)
    elif request.url.query:
        texto_pedido = _resumir_cuerpo(f"?{request.url.query}".encode())
    else:
        texto_pedido = None

    response = await call_next(request)

    cuerpo_respuesta = b"".join([chunk async for chunk in response.body_iterator])
    texto_respuesta = _resumir_cuerpo(cuerpo_respuesta)

    # call_next devuelve una respuesta cuyo body_iterator ya se consumió
    # arriba: hay que reconstruirla con los mismos bytes para poder
    # devolverla igual al consumidor real.
    nueva_respuesta = Response(
        content=cuerpo_respuesta,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
        background=response.background,
    )

    await run_in_threadpool(
        _guardar_log,
        ip=ip_del_cliente(request) or "",
        metodo=f"{request.method} {request.url.path}",
        request_txt=texto_pedido,
        response_code=response.status_code,
        response_txt=texto_respuesta,
    )

    return nueva_respuesta
