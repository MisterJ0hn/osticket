import hmac
import ipaddress
import logging
from typing import List, Optional, Union

from fastapi import HTTPException, Request, status

from app.core.config import settings

logger = logging.getLogger(__name__)

Red = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def _redes_permitidas() -> List[Red]:
    redes = []
    for entrada in settings.ips_permitidas:
        try:
            # strict=False acepta tanto "10.0.0.5" como "10.0.0.0/24".
            redes.append(ipaddress.ip_network(entrada, strict=False))
        except ValueError:
            logger.warning("Entrada inválida en IPS_PERMITIDAS: %s", entrada)
    return redes


def _redes_proxies() -> List[Red]:
    redes = []
    for entrada in settings.trusted_proxies:
        try:
            redes.append(ipaddress.ip_network(entrada, strict=False))
        except ValueError:
            logger.warning("Entrada inválida en TRUSTED_PROXIES: %s", entrada)
    return redes


# Se calculan una vez: parsear las listas en cada request es trabajo inútil
# y estas no cambian sin reiniciar el proceso.
_REDES_PERMITIDAS = _redes_permitidas()
_REDES_PROXIES = _redes_proxies()


def _en_alguna_red(ip: str, redes: List[Red]) -> bool:
    try:
        direccion = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(direccion in red for red in redes)


def ip_del_cliente(request: Request) -> Optional[str]:
    """IP real del que llama.

    Solo se hace caso a X-Forwarded-For cuando la conexión viene de un proxy
    declarado en TRUSTED_PROXIES. Confiar en la cabecera siempre volvería
    decorativa la allowlist: cualquiera la falsifica.
    """
    ip_conexion = request.client.host if request.client else None
    if not ip_conexion:
        return None

    if _REDES_PROXIES and _en_alguna_red(ip_conexion, _REDES_PROXIES):
        reenviada = request.headers.get("x-forwarded-for")
        if reenviada:
            # El primer elemento de la cadena es el cliente original.
            return reenviada.split(",")[0].strip()

    return ip_conexion


def verificar_acceso(request: Request) -> str:
    """Dependencia de seguridad: allowlist de IP + X-API-Key.

    Devuelve la IP del cliente porque la creación de tickets se la pasa a
    osTicket como `ip` del ticket.
    """
    ip = ip_del_cliente(request)

    if _REDES_PERMITIDAS and not _en_alguna_red(ip or "", _REDES_PERMITIDAS):
        logger.warning("Acceso rechazado: IP no autorizada (%s) en %s",
                       ip, request.url.path)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso denegado")

    if not settings.api_keys:
        # Arrancar sin claves configuradas dejaría la API abierta sin que
        # nadie lo note. Se prefiere fallar de forma explícita.
        logger.error("API_KEYS está vacío: no hay ninguna clave configurada")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso denegado")

    clave = request.headers.get("x-api-key", "")
    # compare_digest para no filtrar la clave por el tiempo de comparación.
    if not any(hmac.compare_digest(clave, valida) for valida in settings.api_keys):
        logger.warning("Acceso rechazado: X-API-Key inválida desde %s en %s",
                       ip, request.url.path)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso denegado")

    return ip or ""
