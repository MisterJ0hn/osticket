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

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import obtener_conexion  # noqa: E402
from app.main import app  # noqa: E402

CLAVE = "clave-de-prueba"
CABECERAS = {"X-API-Key": CLAVE}


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
