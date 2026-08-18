"""Configuración común de los tests.

Las variables de entorno se fijan ANTES de importar `app`: tanto Settings
como la allowlist de IPs de `core.security` se resuelven en tiempo de import,
así que un .env de desarrollo cambiaría el resultado de los tests.
"""

import os

os.environ.setdefault("API_KEYS", "clave-de-prueba")
# Vacío = sin filtro por IP, que es lo que necesita el TestClient.
os.environ["IPS_PERMITIDAS"] = ""
os.environ.setdefault("OSTICKET_API_KEY", "clave-osticket")
os.environ.setdefault("OSTICKET_TABLE_PREFIX", "ost_")

import ipaddress  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import security  # noqa: E402
from app.core.database import obtener_conexion  # noqa: E402
from app.core.security import (  # noqa: E402
    ORG_TODAS,
    PERMISO_CREAR,
    PERMISO_LEER,
    PERMISO_NOTAS,
    ClienteApi,
)
from app.main import app  # noqa: E402

# ─────────────────────────────────────────────────────────────
# Clientes de prueba
# ─────────────────────────────────────────────────────────────
# La clave "de siempre" es un cliente interno (ORG_TODAS) para que los tests
# que ya existían sigan viendo todo el helpdesk. El aislamiento se prueba con
# los de organización 2 y 3.

CLAVE = "clave-de-prueba"
CLAVE_A = "clave-cliente-a"
CLAVE_B = "clave-cliente-b"
CLAVE_SIN_NOTAS = "clave-sin-notas"
CLAVE_SOLO_CREAR = "clave-solo-crear"
CLAVE_CON_IPS = "clave-con-ips"

ORG_A = 2
ORG_B = 3

CABECERAS = {"X-API-Key": CLAVE}


def cabeceras(clave: str) -> dict:
    return {"X-API-Key": clave}


TODOS = frozenset({PERMISO_CREAR, PERMISO_LEER, PERMISO_NOTAS})

_CLIENTES = [
    (CLAVE, "interno", ORG_TODAS, TODOS, ()),
    (CLAVE_A, "cliente-a", ORG_A, TODOS, ()),
    (CLAVE_B, "cliente-b", ORG_B, TODOS, ()),
    (CLAVE_SIN_NOTAS, "sin-notas", ORG_A,
     frozenset({PERMISO_CREAR, PERMISO_LEER}), ()),
    (CLAVE_SOLO_CREAR, "solo-crear", ORG_A, frozenset({PERMISO_CREAR}), ()),
    (CLAVE_CON_IPS, "con-ips", ORG_A, TODOS,
     (ipaddress.ip_network("10.9.9.0/24"),)),
]


@pytest.fixture(autouse=True)
def clientes_api(monkeypatch):
    """Rellena la caché de clientes a mano, sin pasar por MySQL.

    `_cargar_clientes` consulta la tabla api_cliente, y en los tests no hay
    base: se inyecta la caché ya armada y se le pone una expiración lejana
    para que no intente refrescarla.
    """
    cache = {}
    for clave, nombre, org_id, permisos, redes in _CLIENTES:
        hash_ = security.hash_clave(clave)
        cache[hash_] = ClienteApi(
            nombre=nombre, org_id=org_id, permisos=permisos,
            redes=redes, clave_hash=hash_,
        )

    monkeypatch.setattr(security, "_cache", cache)
    monkeypatch.setattr(security, "_cache_expira", time.monotonic() + 3600)
    # ultimo_uso escribe en la base: no hay dónde, y no es lo que se prueba.
    monkeypatch.setattr(security, "_registrar_uso", lambda nombre: None)
    yield cache


class ConexionFalsa:
    """Sustituto de la conexión: los tests parchean el repositorio, así que
    nadie llega a ejecutar SQL de verdad."""


@pytest.fixture
def cliente():
    app.dependency_overrides[obtener_conexion] = lambda: ConexionFalsa()
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()


@pytest.fixture
def cliente_sin_auth():
    """Cliente que no manda X-API-Key, para probar el rechazo."""
    with TestClient(app) as tc:
        yield tc
