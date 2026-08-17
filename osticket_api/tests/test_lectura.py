from datetime import datetime

import pytest

from app.services import ticket_service
from tests.conftest import CABECERAS

USUARIO = {"id": 5, "nombre": "Cliente Prueba", "email": "cliente@ejemplo.cl"}

TICKET = {
    "ticket_id": 17,
    "numero": "483920",
    "asunto": "No puedo entrar",
    "estado": "Open",
    "state": "open",
    "abierto": True,
    "prioridad": "Normal",
    "departamento": "Support",
    "tema": "Soporte",
    "agente_asignado": "Jonathan Romero",
    "usuario": USUARIO,
    "creado": datetime(2026, 8, 1, 10, 0, 0),
    "actualizado": datetime(2026, 8, 2, 9, 0, 0),
    "cerrado": None,
    "vencimiento": None,
    "atrasado": False,
    "respondido": False,
}


@pytest.fixture
def repo(monkeypatch):
    """Repositorio parcheado: los tests validan el mapeo y el ruteo, no el SQL."""
    class Falso:
        pass

    falso = Falso()
    monkeypatch.setattr(ticket_service, "ticket_repo", falso)
    return falso


def test_listar_tickets_del_usuario(cliente, repo):
    repo.buscar_usuario_por_email = lambda conexion, email: USUARIO
    repo.listar_tickets_de_usuario = lambda conexion, **kw: (1, [TICKET])

    respuesta = cliente.get(
        "/api/v1/tickets?email=cliente@ejemplo.cl", headers=CABECERAS
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    assert cuerpo["tickets"][0]["numero"] == "483920"
    assert cuerpo["tickets"][0]["abierto"] is True
    assert cuerpo["tickets"][0]["usuario"]["email"] == "cliente@ejemplo.cl"


def test_listar_filtra_por_estado_abiertos_por_defecto(cliente, repo):
    recibido = {}

    def espia(conexion, **kw):
        recibido.update(kw)
        return 0, []

    repo.buscar_usuario_por_email = lambda conexion, email: USUARIO
    repo.listar_tickets_de_usuario = espia

    cliente.get("/api/v1/tickets?email=cliente@ejemplo.cl", headers=CABECERAS)
    assert recibido["estado"].value == "abiertos"
    assert recibido["user_id"] == 5


def test_usuario_inexistente_devuelve_404(cliente, repo):
    repo.buscar_usuario_por_email = lambda conexion, email: None

    respuesta = cliente.get("/api/v1/tickets?email=nadie@ejemplo.cl", headers=CABECERAS)
    assert respuesta.status_code == 404
    assert respuesta.json()["exito"] is False


def test_detalle_incluye_el_hilo(cliente, repo):
    detalle = {**TICKET, "fuente": "API", "ip_origen": "127.0.0.1",
               "ultima_respuesta": None, "ultimo_mensaje": None, "_thread_id": 3}
    repo.obtener_ticket = lambda conexion, numero: dict(detalle)
    repo.obtener_hilo = lambda conexion, thread_id, incluir_notas: [
        {"id": 1, "tipo": "mensaje", "autor": "Cliente Prueba", "titulo": "No puedo entrar",
         "cuerpo": "Me dice contraseña incorrecta", "formato": "text",
         "creado": datetime(2026, 8, 1, 10, 0, 0), "adjuntos": []}
    ]

    respuesta = cliente.get("/api/v1/tickets/483920", headers=CABECERAS)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["mensajes"][0]["tipo"] == "mensaje"
    # El campo interno con el id del hilo no debe filtrarse en la respuesta.
    assert "_thread_id" not in cuerpo


def test_detalle_de_ticket_inexistente_devuelve_404(cliente, repo):
    repo.obtener_ticket = lambda conexion, numero: None

    respuesta = cliente.get("/api/v1/tickets/000000", headers=CABECERAS)
    assert respuesta.status_code == 404


def test_estado_de_ticket(cliente, repo):
    repo.obtener_estado = lambda conexion, numero: {
        "numero": "483920", "estado": "Closed", "state": "closed", "abierto": False,
        "agente_asignado": None, "creado": datetime(2026, 8, 1, 10, 0),
        "actualizado": None, "cerrado": datetime(2026, 8, 3, 12, 0),
        "vencimiento": None, "atrasado": False,
    }

    respuesta = cliente.get("/api/v1/tickets/483920/estado", headers=CABECERAS)
    assert respuesta.status_code == 200
    assert respuesta.json()["abierto"] is False
    assert respuesta.json()["estado"] == "Closed"
