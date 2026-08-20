import base64
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
        return {"mensaje_id": 321, "adjuntos_guardados": 0}

    escritura.responder_ticket = responder

    respuesta = cliente.post(
        "/api/v1/tickets/483920/mensajes",
        json={"email": "cliente@ejemplo.cl", "mensaje": "Gracias, ya funciona"},
        headers=CABECERAS,
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo == {"exito": True, "numero": "483920", "mensaje_id": 321,
                      "adjuntos_guardados": 0}
    assert llamada["ticket_id"] == 17
    assert llamada["thread_id"] == 99
    assert llamada["user_id"] == 5
    assert llamada["mensaje"] == "Gracias, ya funciona"
    assert llamada["adjuntos"] == []


def test_responder_con_una_imagen_adjunta(cliente, repos):
    lectura, escritura = repos
    lectura.obtener_ticket = lambda conexion, numero, **kw: TICKET
    llamada = {}

    def responder(conexion, **kw):
        llamada.update(kw)
        return {"mensaje_id": 322, "adjuntos_guardados": len(kw["adjuntos"])}

    escritura.responder_ticket = responder

    contenido = base64.b64encode(b"contenido-de-la-imagen").decode()
    respuesta = cliente.post(
        "/api/v1/tickets/483920/mensajes",
        json={
            "email": "cliente@ejemplo.cl",
            "mensaje": "Les mando una foto",
            "adjuntos": [
                {"nombre": "foto.png", "contenido_base64": contenido,
                 "tipo_mime": "image/png"}
            ],
        },
        headers=CABECERAS,
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["adjuntos_guardados"] == 1
    assert llamada["adjuntos"] == [
        {"nombre": "foto.png", "tipo_mime": "image/png",
         "contenido": b"contenido-de-la-imagen"}
    ]


def test_responder_con_adjunto_no_base64_da_400(cliente, repos):
    lectura, escritura = repos
    lectura.obtener_ticket = lambda conexion, numero, **kw: TICKET
    escritura.responder_ticket = lambda conexion, **kw: pytest.fail(
        "no debería escribir si el adjunto no es base64 válido"
    )

    respuesta = cliente.post(
        "/api/v1/tickets/483920/mensajes",
        json={
            "email": "cliente@ejemplo.cl",
            "mensaje": "Les mando una foto",
            "adjuntos": [
                {"nombre": "foto.png", "contenido_base64": "no-es-base64!!",
                 "tipo_mime": "image/png"}
            ],
        },
        headers=CABECERAS,
    )
    assert respuesta.status_code == 400


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
