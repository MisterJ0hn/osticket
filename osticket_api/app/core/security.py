"""Autenticación y autorización de los consumidores de esta API.

Cada cliente tiene su propia clave, dada de alta en la tabla `api_cliente`
(ver sql/001_api_cliente.sql y scripts/gestionar_clientes.py). La clave
identifica al cliente y, sobre todo, determina qué datos puede ver: su
`org_id` es el que limita las consultas a los tickets de esa organización.
"""

import hashlib
import hmac
import ipaddress
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple, Union

from fastapi import HTTPException, Request, status

from app.core.config import settings

logger = logging.getLogger(__name__)

Red = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# Permisos reconocidos. Un cliente sin el permiso correspondiente no puede
# ejecutar esa operación.
PERMISO_CREAR = "crear"
PERMISO_LEER = "leer"
PERMISO_NOTAS = "notas"

# org_id 0 = cliente interno: ve todos los tickets, sin filtro de
# organización. Es la única forma de saltarse el aislamiento.
ORG_TODAS = 0


@dataclass(frozen=True)
class ClienteApi:
    """El consumidor autenticado, tal como lo ven las rutas."""

    nombre: str
    # ost_organization.id que delimita sus datos. ORG_TODAS = sin filtro.
    org_id: int
    permisos: frozenset
    # IP de origen. La creación de tickets se la pasa a osTicket como `ip`
    # del ticket, que es para lo que verificar_acceso() la devolvía antes.
    ip: str = ""
    redes: Tuple[Red, ...] = field(default=(), compare=False)
    # Hash con el que está indexado en la caché. Se guarda para poder
    # confirmar la coincidencia con compare_digest sin volver a buscarlo.
    clave_hash: str = field(default="", compare=False, repr=False)

    @property
    def ve_todo(self) -> bool:
        return self.org_id == ORG_TODAS

    def puede(self, permiso: str) -> bool:
        return permiso in self.permisos


def _redes(entradas: List[str], origen: str) -> List[Red]:
    redes = []
    for entrada in entradas:
        try:
            # strict=False acepta tanto "10.0.0.5" como "10.0.0.0/24".
            redes.append(ipaddress.ip_network(entrada, strict=False))
        except ValueError:
            logger.warning("Entrada inválida en %s: %s", origen, entrada)
    return redes


# Se calculan una vez: parsear las listas del .env en cada request es trabajo
# inútil y estas no cambian sin reiniciar el proceso.
_REDES_PERMITIDAS = _redes(settings.ips_permitidas, "IPS_PERMITIDAS")
_REDES_PROXIES = _redes(settings.trusted_proxies, "TRUSTED_PROXIES")


def _en_alguna_red(ip: str, redes) -> bool:
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


def hash_clave(clave: str) -> str:
    """sha256 en hex de una clave.

    sha256 y no bcrypt a propósito: estas claves las genera el sistema con
    256 bits de entropía (secrets.token_urlsafe), no las elige una persona.
    Un hash lento defiende contra diccionarios sobre contraseñas humanas;
    acá no hay nada que adivinar.
    """
    return hashlib.sha256(clave.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────
# Caché de clientes
# ─────────────────────────────────────────────────────────────
# Consultar la tabla en cada request sería una consulta por petición para un
# dato que casi nunca cambia. Con TTL, una baja (activo = 0) surte efecto en
# ≤ CACHE_CLIENTES_TTL segundos sin reiniciar el servicio, que es justamente
# lo que se buscaba al mover las claves del .env a la base.

_cache: Dict[str, ClienteApi] = {}
_cache_expira: float = 0.0
# Las rutas se declaran con `def`, así que FastAPI las corre en su threadpool
# y varias pueden intentar refrescar a la vez.
_cache_lock = threading.Lock()

# Acelerador de ultimo_uso: cuándo se escribió por última vez cada cliente.
_ultimo_uso: Dict[str, float] = {}
_INTERVALO_ULTIMO_USO = 300.0  # 5 min


def _clientes_del_env() -> Dict[str, ClienteApi]:
    """Compatibilidad con la variable API_KEYS del .env.

    Son claves anónimas y sin organización: ven TODO el helpdesk, igual que
    antes de que existiera api_cliente. Siguen funcionando para poder
    desplegar sin cortarles el servicio a los consumidores que ya estaban
    andando, y el arranque avisa de cada una (ver main.lifespan). Una vez
    migrados a api_cliente, se vacía API_KEYS.
    """
    clientes = {}
    for indice, clave in enumerate(settings.api_keys, start=1):
        hash_ = hash_clave(clave)
        clientes[hash_] = ClienteApi(
            nombre=f"legacy-{indice}",
            org_id=ORG_TODAS,
            permisos=frozenset({PERMISO_CREAR, PERMISO_LEER, PERMISO_NOTAS}),
            clave_hash=hash_,
        )
    return clientes


def _cargar_clientes() -> Dict[str, ClienteApi]:
    """Arma el índice hash-de-clave -> cliente, desde el .env y la base."""
    # Import local: core.database crea el engine al importarse y a este
    # módulo lo importan los routers. A nivel de módulo sería un ciclo.
    from app.core import database
    from app.repositories import cliente_repo

    clientes = _clientes_del_env()

    with database.engine.connect() as conexion:
        for fila in cliente_repo.listar_activos(conexion):
            permisos = frozenset(
                p.strip() for p in fila["permisos"].split(",") if p.strip()
            )
            ips = [ip.strip() for ip in fila["ips_permitidas"].split(",") if ip.strip()]
            nombre = fila["nombre"]
            hash_ = fila["clave_hash"].strip().lower()
            clientes[hash_] = ClienteApi(
                nombre=nombre,
                org_id=fila["org_id"],
                permisos=permisos,
                redes=tuple(_redes(ips, "ips_permitidas de " + nombre)),
                clave_hash=hash_,
            )

    return clientes


def _clientes() -> Dict[str, ClienteApi]:
    """Devuelve la caché, refrescándola si venció."""
    global _cache, _cache_expira

    if _cache_expira > time.monotonic():
        return _cache

    with _cache_lock:
        # Otro hilo pudo refrescarla mientras se esperaba el lock.
        if _cache_expira > time.monotonic():
            return _cache
        try:
            _cache = _cargar_clientes()
            _cache_expira = time.monotonic() + settings.CACHE_CLIENTES_TTL
        except Exception:
            # A propósito se sigue sirviendo la caché vencida: si la base no
            # responde, vaciarla dejaría a TODOS los clientes sin autenticar,
            # convirtiendo un problema de lectura en una caída total de la
            # API. Se reintenta en la siguiente petición.
            logger.exception(
                "No se pudo refrescar la caché de clientes; se sigue usando la "
                "anterior (%s clientes)", len(_cache),
            )
        return _cache


def invalidar_cache() -> None:
    """Fuerza a releer la tabla en la próxima petición."""
    global _cache_expira
    _cache_expira = 0.0


def _registrar_uso(nombre: str) -> None:
    """Marca ultimo_uso, como mucho una vez cada _INTERVALO_ULTIMO_USO."""
    if nombre.startswith("legacy-"):
        # Las claves del .env no tienen fila donde anotarlo.
        return

    ahora = time.monotonic()
    if ahora - _ultimo_uso.get(nombre, 0.0) < _INTERVALO_ULTIMO_USO:
        return
    _ultimo_uso[nombre] = ahora

    from app.core import database
    from app.repositories import cliente_repo

    try:
        with database.transaccion() as conexion:
            cliente_repo.marcar_uso(conexion, nombre)
    except Exception:
        # Es un dato de diagnóstico: que no se pueda escribir no es motivo
        # para rechazar una petición que por lo demás es válida.
        logger.warning("No se pudo actualizar ultimo_uso de %s", nombre)


# ─────────────────────────────────────────────────────────────
# Dependencias de FastAPI
# ─────────────────────────────────────────────────────────────

def verificar_acceso(request: Request) -> ClienteApi:
    """Autentica al cliente y devuelve su identidad.

    El objeto que devuelve lleva el `org_id`, que es lo que las consultas
    usan para no mostrarle a un cliente los tickets de otro.
    """
    ip = ip_del_cliente(request) or ""

    # 1. Allowlist global del .env: reja exterior, igual para todos.
    if _REDES_PERMITIDAS and not _en_alguna_red(ip, _REDES_PERMITIDAS):
        logger.warning("Acceso rechazado: IP no autorizada (%s) en %s",
                       ip, request.url.path)
        raise _denegado()

    clientes = _clientes()
    if not clientes:
        # Quedarse sin ningún cliente activo dejaría la API muerta sin que
        # nadie lo note. Se prefiere dejar rastro explícito en el log.
        logger.error(
            "No hay clientes activos: revisar la tabla api_cliente "
            "(¿se corrió sql/001_api_cliente.sql?)"
        )
        raise _denegado()

    clave = request.headers.get("x-api-key", "")
    hash_presentado = hash_clave(clave)

    # 2. Lookup por el hash de la clave. Indexar por hash evita recorrer todos
    # los clientes en cada petición; compare_digest confirma la coincidencia
    # sin filtrar nada por el tiempo de comparación.
    cliente = clientes.get(hash_presentado) if clave else None
    if cliente is None or not hmac.compare_digest(hash_presentado, cliente.clave_hash):
        logger.warning("Acceso rechazado: X-API-Key inválida desde %s en %s",
                       ip, request.url.path)
        raise _denegado()

    # 3. Allowlist propia del cliente. Va DESPUÉS de identificarlo: hasta no
    # saber quién es, no se sabe qué IPs le corresponden.
    if cliente.redes and not _en_alguna_red(ip, cliente.redes):
        logger.warning("Acceso rechazado: %s desde IP no autorizada (%s)",
                       cliente.nombre, ip)
        raise _denegado()

    _registrar_uso(cliente.nombre)

    # El cacheado no lleva IP (es del request, no del cliente): se copia
    # poniéndosela. replace() sobre un frozen dataclass devuelve uno nuevo,
    # así que la caché no se toca.
    return replace(cliente, ip=ip)


def exigir(permiso: str):
    """Dependencia que exige un permiso concreto, además de autenticar.

    Se usa en cada ruta: `crear` en el POST y `leer` en los GET. Un cliente
    puede tener uno sin el otro (un sistema que solo abre tickets no necesita
    poder leer el helpdesk).
    """

    def dependencia(request: Request) -> ClienteApi:
        cliente = verificar_acceso(request)
        if not cliente.puede(permiso):
            logger.warning("%s no tiene el permiso '%s' (%s)",
                           cliente.nombre, permiso, request.url.path)
            raise _denegado()
        return cliente

    return dependencia


def _denegado() -> HTTPException:
    """Una sola respuesta para todos los rechazos.

    IP no autorizada, clave inválida, cliente dado de baja o permiso faltante
    contestan lo mismo: distinguirlos le diría a quien sondea qué parte acertó.
    """
    return HTTPException(status.HTTP_403_FORBIDDEN, "Acceso denegado")
