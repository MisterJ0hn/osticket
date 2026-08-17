import base64
from contextlib import contextmanager

import pytest

from app.core.exceptions import OsticketApiError
from app.repositories import ticket_write_repo
from app.schemas.ticket import Adjunto
from app.services import osticket_client, ticket_service
from tests.conftest import CABECERAS

TICKET_NUEVO = {
    "email": "cliente@ejemplo.cl",
    "nombre": "Cliente Prueba",
    "asunto": "No puedo entrar al sistema",
    "mensaje": "Me dice usuario o contraseña incorrectos.",
}


@pytest.fixture(autouse=True)
def sin_base(monkeypatch):
    """Ni la resolución del id ni el fallback deben tocar MySQL en los tests."""
    monkeypatch.setattr(ticket_service, "_resolver_ticket_id", lambda numero: 17)

    @contextmanager
    def transaccion_falsa():
        yield None

    monkeypatch.setattr(ticket_service.database, "transaccion", transaccion_falsa)


def test_creacion_por_api_nativa(cliente, monkeypatch):
    monkeypatch.setattr(osticket_client, "crear_ticket", lambda payload: "483920")

    respuesta = cliente.post("/api/v1/tickets", json=TICKET_NUEVO, headers=CABECERAS)
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo == {"exito": True, "numero": "483920", "ticket_id": 17,
                      "origen": "nativa", "mensaje": None}


def test_error_de_transporte_cae_al_fallback(cliente, monkeypatch):
    def revienta(payload):
        raise OsticketApiError("timeout", es_transporte=True)

    monkeypatch.setattr(osticket_client, "crear_ticket", revienta)
    monkeypatch.setattr(
        ticket_write_repo, "crear_ticket",
        lambda conexion, **kw: {"numero": "999111", "ticket_id": 42, "user_id": 5},
    )

    respuesta = cliente.post("/api/v1/tickets", json=TICKET_NUEVO, headers=CABECERAS)
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["origen"] == "fallback_sql"
    assert cuerpo["numero"] == "999111"
    assert "notificaciones" in cuerpo["mensaje"]


def test_error_de_validacion_no_cae_al_fallback(cliente, monkeypatch):
    """Un 4xx de osTicket es problema de los datos: repetir por SQL solo
    saltaría la validación."""
    def revienta(payload):
        raise OsticketApiError("Falta el asunto", status_code=400, es_transporte=False)

    llamadas = []
    monkeypatch.setattr(osticket_client, "crear_ticket", revienta)
    monkeypatch.setattr(ticket_write_repo, "crear_ticket",
                        lambda conexion, **kw: llamadas.append(kw))

    respuesta = cliente.post("/api/v1/tickets", json=TICKET_NUEVO, headers=CABECERAS)
    assert respuesta.status_code == 400
    assert llamadas == []


def test_fallback_desactivado_devuelve_502(cliente, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "OSTICKET_FALLBACK_SQL", False)
    monkeypatch.setattr(
        osticket_client, "crear_ticket",
        lambda payload: (_ for _ in ()).throw(OsticketApiError("caído", es_transporte=True)),
    )

    respuesta = cliente.post("/api/v1/tickets", json=TICKET_NUEVO, headers=CABECERAS)
    assert respuesta.status_code == 502


def test_email_invalido_devuelve_422(cliente):
    respuesta = cliente.post(
        "/api/v1/tickets", json={**TICKET_NUEVO, "email": "no-es-un-email"},
        headers=CABECERAS,
    )
    assert respuesta.status_code == 422


def test_payload_usa_el_formato_de_adjuntos_del_parser_json():
    """osTicket espera [{"nombre": "data:mime;base64,..."}], no {name,data}."""
    contenido = base64.b64encode(b"hola").decode()
    payload = osticket_client.construir_payload(
        email="cliente@ejemplo.cl",
        asunto="Asunto",
        mensaje="Cuerpo",
        adjuntos=[Adjunto(nombre="nota.txt", contenido_base64=contenido,
                          tipo_mime="text/plain")],
    )
    assert payload["attachments"] == [
        {"nota.txt": f"data:text/plain;base64,{contenido}"}
    ]


def test_un_500_de_validacion_de_osticket_no_es_error_de_transporte(monkeypatch):
    """osTicket responde 500 a los errores de validación de Ticket::create()
    (api.tickets.php:172). Tratarlos como caída haría que el fallback creara
    el ticket saltándose la validación."""
    class RespuestaFalsa:
        status_code = 500
        text = "Unable to create new ticket :user\nIncomplete client information"

    monkeypatch.setattr(osticket_client.httpx, "post",
                        lambda *a, **kw: RespuestaFalsa())
    monkeypatch.setattr(osticket_client.settings, "OSTICKET_API_KEY", "x")

    with pytest.raises(OsticketApiError) as excinfo:
        osticket_client.crear_ticket({"email": "a@b.cl"})
    assert excinfo.value.es_transporte is False


def test_un_500_sin_la_marca_de_osticket_si_es_de_transporte(monkeypatch):
    """Un 500 de Apache o un fatal de PHP sí es una caída: ahí el fallback
    tiene sentido."""
    class RespuestaFalsa:
        status_code = 500
        text = "<html><title>500 Internal Server Error</title></html>"

    monkeypatch.setattr(osticket_client.httpx, "post",
                        lambda *a, **kw: RespuestaFalsa())
    monkeypatch.setattr(osticket_client.settings, "OSTICKET_API_KEY", "x")

    with pytest.raises(OsticketApiError) as excinfo:
        osticket_client.crear_ticket({"email": "a@b.cl"})
    assert excinfo.value.es_transporte is True


def test_payload_rechaza_base64_invalido():
    with pytest.raises(OsticketApiError):
        osticket_client.construir_payload(
            email="cliente@ejemplo.cl", asunto="a", mensaje="b",
            adjuntos=[Adjunto(nombre="malo.txt", contenido_base64="no-es-base64!!")],
        )
