from contextlib import contextmanager

import pytest

from app.services import ticket_service
from tests.conftest import CABECERAS, cabeceras

USUARIO = {"id": 5, "nombre": "Cliente Prueba", "email": "cliente@ejemplo.cl"}

TICKET = {
    "ticket_id": 17,
    "numero": "483920",
    "_thread_id": 99,
    "usuario": USUARIO,
}


@pytest.fixture(autouse=True)
def sin_base(monkeypatch):
    """La transacción no debe tocar MySQL: los tests solo prueban ruteo y validación."""
    @contextmanager
    def transaccion_falsa():
        yield None

    monkeypatch.setattr(ticket_service.database, "transaccion", transaccion_falsa)


@pytest.fixture
def repos(monkeypatch):
    class FalsoLectura:
        pass

    class FalsoEscritura:
        pass

    lectura, escritura = FalsoLectura(), FalsoEscritura()
    monkeypatch.setattr(ticket_service, "ticket_repo", lectura)
    monkeypatch.setattr(ticket_service, "ticket_write_repo", escritura)
    return lectura, escritura


def test_responder_agrega_el_mensaje(cliente, repos):
    lectura, escritura = repos
    lectura.obtener_ticket = lambda conexion, numero, **kw: TICKET
    llamada = {}

    def responder(conexion, **kw):
        llamada.update(kw)
        return {"mensaje_id": 321}

    escritura.responder_ticket = responder

    respuesta = cliente.post(
        "/api/v1/tickets/483920/mensajes",
        json={"email": "cliente@ejemplo.cl", "mensaje": "Gracias, ya funciona"},
        headers=CABECERAS,
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo == {"exito": True, "numero": "483920", "mensaje_id": 321}
    assert llamada["ticket_id"] == 17
    assert llamada["thread_id"] == 99
    assert llamada["user_id"] == 5
    assert llamada["mensaje"] == "Gracias, ya funciona"


def test_responder_ticket_inexistente_da_404(cliente, repos):
    lectura, _ = repos
    lectura.obtener_ticket = lambda conexion, numero, **kw: None

    respuesta = cliente.post(
        "/api/v1/tickets/000000/mensajes",
        json={"email": "cliente@ejemplo.cl", "mensaje": "Hola"},
        headers=CABECERAS,
    )
    assert respuesta.status_code == 404


def test_responder_con_email_de_otro_dueno_da_400(cliente, repos):
    lectura, escritura = repos
    lectura.obtener_ticket = lambda conexion, numero, **kw: TICKET
    escritura.responder_ticket = lambda conexion, **kw: pytest.fail(
        "no debería escribir si el email no coincide"
    )

    respuesta = cliente.post(
        "/api/v1/tickets/483920/mensajes",
        json={"email": "otra-persona@ejemplo.cl", "mensaje": "Hola"},
        headers=CABECERAS,
    )
    assert respuesta.status_code == 400


def test_responder_con_clave_invalida_da_403(cliente, repos):
    respuesta = cliente.post(
        "/api/v1/tickets/483920/mensajes",
        json={"email": "cliente@ejemplo.cl", "mensaje": "Hola"},
        headers=cabeceras("clave-invalida"),
    )
    assert respuesta.status_code == 403
