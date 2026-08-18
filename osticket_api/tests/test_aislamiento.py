"""Aislamiento entre clientes de la API.

Cada cliente tiene su clave y su organización (ost_organization). Lo que se
prueba acá es que esa organización realmente limite lo que puede leer: es el
motivo de existir de la tabla api_cliente, y un fallo no se nota mirando las
respuestas — se nota cuando un cliente ve los tickets de otro.
"""

from datetime import datetime

import pytest

from app.core import security
from app.services import ticket_service
from tests.conftest import (
    CLAVE,
    CLAVE_A,
    CLAVE_B,
    CLAVE_CON_IPS,
    CLAVE_SIN_NOTAS,
    CLAVE_SOLO_CREAR,
    ORG_A,
    cabeceras,
)

USUARIO_A = {"id": 5, "nombre": "Usuario A", "email": "alguien@empresa-a.cl"}

TICKET_A = {
    "ticket_id": 17,
    "numero": "483920",
    "asunto": "No puedo entrar",
    "estado": "Open",
    "state": "open",
    "abierto": True,
    "prioridad": "Normal",
    "departamento": "Support",
    "tema": "Soporte",
    "agente_asignado": None,
    "usuario": USUARIO_A,
    "creado": datetime(2026, 8, 1, 10, 0, 0),
    "actualizado": None,
    "cerrado": None,
    "vencimiento": None,
    "atrasado": False,
    "respondido": False,
}


@pytest.fixture
def repo(monkeypatch):
    """Repositorio que se comporta como la base: solo devuelve lo de ORG_A.

    Es la parte importante de este archivo. En la base real el filtro lo hace
    el WHERE de la consulta; acá se imita comprobando el org_id que le llega
    al repositorio. Así el test falla si una ruta se olvida de bajarlo, que
    es exactamente el error que se quiere impedir.
    """
    class Falso:
        def buscar_usuario_por_email(self, conexion, email, org_id=0):
            if email != USUARIO_A["email"]:
                return None
            # org_id 0 = cliente interno: sin filtro.
            if org_id not in (0, ORG_A):
                return None
            return USUARIO_A

        def listar_tickets_de_usuario(self, conexion, user_id, org_id=0, **kw):
            if org_id not in (0, ORG_A):
                return 0, []
            return 1, [TICKET_A]

        def obtener_ticket(self, conexion, numero, org_id=0):
            if numero != TICKET_A["numero"] or org_id not in (0, ORG_A):
                return None
            return {**TICKET_A, "fuente": "API", "ip_origen": "127.0.0.1",
                    "ultima_respuesta": None, "ultimo_mensaje": None,
                    "_thread_id": 3}

        def obtener_estado(self, conexion, numero, org_id=0):
            if numero != TICKET_A["numero"] or org_id not in (0, ORG_A):
                return None
            return {"numero": numero, "estado": "Open", "state": "open",
                    "abierto": True, "agente_asignado": None,
                    "creado": TICKET_A["creado"], "actualizado": None,
                    "cerrado": None, "vencimiento": None, "atrasado": False}

        def obtener_hilo(self, conexion, thread_id, incluir_notas):
            mensajes = [{
                "id": 1, "tipo": "mensaje", "autor": "Usuario A",
                "titulo": "No puedo entrar", "cuerpo": "Sale error",
                "formato": "text", "creado": TICKET_A["creado"], "adjuntos": [],
            }]
            if incluir_notas:
                mensajes.append({
                    "id": 2, "tipo": "nota", "autor": "Agente",
                    "titulo": "Interno", "cuerpo": "El cliente insiste",
                    "formato": "text", "creado": TICKET_A["creado"], "adjuntos": [],
                })
            return mensajes

    falso = Falso()
    monkeypatch.setattr(ticket_service, "ticket_repo", falso)
    return falso


# ─────────────────────────────────────────────────────────────
# Lo propio se ve
# ─────────────────────────────────────────────────────────────

def test_cliente_ve_los_tickets_de_su_organizacion(cliente, repo):
    respuesta = cliente.get(
        f"/api/v1/tickets?email={USUARIO_A['email']}", headers=cabeceras(CLAVE_A)
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["tickets"][0]["numero"] == "483920"


def test_cliente_ve_el_detalle_de_su_ticket(cliente, repo):
    respuesta = cliente.get("/api/v1/tickets/483920", headers=cabeceras(CLAVE_A))
    assert respuesta.status_code == 200
    assert respuesta.json()["numero"] == "483920"


# ─────────────────────────────────────────────────────────────
# Lo ajeno no se ve, y no se distingue de lo inexistente
# ─────────────────────────────────────────────────────────────

def test_listado_de_otra_organizacion_devuelve_404(cliente, repo):
    """Un email de otra organización responde igual que uno que no existe.

    Distinguirlos le confirmaría a un cliente que cierta persona está
    registrada en el helpdesk de otro.
    """
    ajeno = cliente.get(
        f"/api/v1/tickets?email={USUARIO_A['email']}", headers=cabeceras(CLAVE_B)
    )
    inexistente = cliente.get(
        "/api/v1/tickets?email=nadie@ejemplo.cl", headers=cabeceras(CLAVE_B)
    )
    assert ajeno.status_code == inexistente.status_code == 404
    assert ajeno.json()["exito"] is False


def test_detalle_de_otra_organizacion_devuelve_404(cliente, repo):
    """404 y no 403: un 403 confirmaría que ese número de ticket existe."""
    respuesta = cliente.get("/api/v1/tickets/483920", headers=cabeceras(CLAVE_B))
    assert respuesta.status_code == 404


def test_estado_de_otra_organizacion_devuelve_404(cliente, repo):
    """El endpoint de estado es el más fácil de olvidar: su consulta no
    necesitaba ost_user para nada, el join está solo para poder filtrar."""
    respuesta = cliente.get("/api/v1/tickets/483920/estado", headers=cabeceras(CLAVE_B))
    assert respuesta.status_code == 404


def test_cliente_interno_ve_todas_las_organizaciones(cliente, repo):
    """org_id 0 es la única forma de saltarse el aislamiento."""
    respuesta = cliente.get(
        f"/api/v1/tickets?email={USUARIO_A['email']}", headers=cabeceras(CLAVE)
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["total"] == 1


# ─────────────────────────────────────────────────────────────
# Notas internas de los agentes
# ─────────────────────────────────────────────────────────────

def test_sin_permiso_de_notas_no_llegan_las_notas(cliente, repo):
    """El filtro por organización no cubre esto: las notas SON de su
    organización, pero las escribe el agente dando por hecho que el cliente
    no las lee."""
    respuesta = cliente.get(
        "/api/v1/tickets/483920?incluir_notas=true", headers=cabeceras(CLAVE_SIN_NOTAS)
    )
    assert respuesta.status_code == 200
    tipos = [m["tipo"] for m in respuesta.json()["mensajes"]]
    assert "nota" not in tipos


def test_con_permiso_de_notas_si_llegan(cliente, repo):
    respuesta = cliente.get(
        "/api/v1/tickets/483920?incluir_notas=true", headers=cabeceras(CLAVE_A)
    )
    assert respuesta.status_code == 200
    tipos = [m["tipo"] for m in respuesta.json()["mensajes"]]
    assert "nota" in tipos


# ─────────────────────────────────────────────────────────────
# Permisos e identidad
# ─────────────────────────────────────────────────────────────

def test_cliente_sin_permiso_de_lectura_no_puede_leer(cliente, repo):
    respuesta = cliente.get(
        f"/api/v1/tickets?email={USUARIO_A['email']}",
        headers=cabeceras(CLAVE_SOLO_CREAR),
    )
    assert respuesta.status_code == 403


def test_cliente_dado_de_baja_pierde_el_acceso(cliente, repo, clientes_api):
    """Una baja es quitarlo de la caché, que es lo que hace el refresco
    periódico al releer la tabla con activo = 1."""
    antes = cliente.get(
        f"/api/v1/tickets?email={USUARIO_A['email']}", headers=cabeceras(CLAVE_A)
    )
    assert antes.status_code == 200

    del clientes_api[security.hash_clave(CLAVE_A)]

    despues = cliente.get(
        f"/api/v1/tickets?email={USUARIO_A['email']}", headers=cabeceras(CLAVE_A)
    )
    assert despues.status_code == 403


def test_ip_fuera_de_la_allowlist_del_cliente(cliente, repo):
    """La allowlist propia se comprueba DESPUÉS de identificar al cliente:
    hasta no saber quién es, no se sabe qué IPs le tocan."""
    respuesta = cliente.get(
        f"/api/v1/tickets?email={USUARIO_A['email']}", headers=cabeceras(CLAVE_CON_IPS)
    )
    assert respuesta.status_code == 403


@pytest.mark.parametrize("ruta", [
    "/api/v1/tickets?email=alguien@ejemplo.cl",
    "/api/v1/tickets/483920",
    "/api/v1/tickets/483920/estado",
    "/api/v1/catalogos/temas",
    "/api/v1/catalogos/estados",
    "/api/v1/catalogos/prioridades",
])
def test_ninguna_ruta_queda_sin_clave(cliente_sin_auth, ruta):
    """Regresión: al bajar la autenticación del router a cada ruta, es fácil
    dejar una sin la dependencia. Este test recorre todas."""
    assert cliente_sin_auth.get(ruta).status_code == 403


def test_post_de_tickets_tambien_exige_clave(cliente_sin_auth):
    respuesta = cliente_sin_auth.post(
        "/api/v1/tickets",
        json={"email": "alguien@ejemplo.cl", "asunto": "x", "mensaje": "y"},
    )
    assert respuesta.status_code == 403


# ─────────────────────────────────────────────────────────────
# Creación: la organización del usuario nuevo
# ─────────────────────────────────────────────────────────────
# Es el punto del que depende todo lo demás. osTicket crea los usuarios que
# no existen y los deja en org_id = 0; si nadie los asigna, el cliente crea
# el ticket correctamente y después NO lo ve, porque las lecturas filtran por
# organización. El síntoma ("lo creé y no aparece") no apunta a la causa.

TICKET_NUEVO = {
    "email": "nuevo@empresa-a.cl",
    "asunto": "Prueba",
    "mensaje": "Contenido",
}


@pytest.fixture
def creacion(monkeypatch):
    """Aísla la creación de MySQL y registra lo que se le pide al repo."""
    from contextlib import contextmanager

    from app.repositories import ticket_write_repo
    from app.services import osticket_client

    registro = {"asignaciones": [], "org_del_email": None, "crear_kw": None}

    @contextmanager
    def conexion_falsa():
        yield None

    class EngineFalso:
        connect = staticmethod(conexion_falsa)

    monkeypatch.setattr(ticket_service.database, "transaccion", conexion_falsa)
    monkeypatch.setattr(ticket_service.database, "engine", EngineFalso)
    monkeypatch.setattr(ticket_service, "_resolver_ticket_id", lambda numero: 17)
    monkeypatch.setattr(osticket_client, "crear_ticket", lambda payload: "483920")

    monkeypatch.setattr(
        ticket_write_repo, "organizacion_del_email",
        lambda conexion, email: registro["org_del_email"],
    )
    monkeypatch.setattr(
        ticket_write_repo, "asignar_organizacion",
        lambda conexion, email, org_id: registro["asignaciones"].append((email, org_id)),
    )

    def crear_falso(conexion, **kw):
        registro["crear_kw"] = kw
        return {"numero": "483920", "ticket_id": 17}

    monkeypatch.setattr(ticket_write_repo, "crear_ticket", crear_falso)
    return registro


def test_creacion_asigna_la_organizacion_del_cliente(cliente, creacion):
    """Sin esto el cliente no puede leer el ticket que acaba de crear."""
    respuesta = cliente.post(
        "/api/v1/tickets", json=TICKET_NUEVO, headers=cabeceras(CLAVE_A)
    )
    assert respuesta.status_code == 201
    assert creacion["asignaciones"] == [("nuevo@empresa-a.cl", ORG_A)]


def test_el_fallback_sql_crea_al_usuario_ya_con_su_organizacion(cliente, creacion,
                                                                monkeypatch):
    from app.core.exceptions import OsticketApiError
    from app.services import osticket_client

    def revienta(payload):
        raise OsticketApiError("timeout", es_transporte=True)

    monkeypatch.setattr(osticket_client, "crear_ticket", revienta)

    respuesta = cliente.post(
        "/api/v1/tickets", json=TICKET_NUEVO, headers=cabeceras(CLAVE_A)
    )
    assert respuesta.status_code == 201
    assert respuesta.json()["origen"] == "fallback_sql"
    # El INSERT del usuario lleva la organización, no un 0.
    assert creacion["crear_kw"]["org_id"] == ORG_A


def test_email_de_otra_organizacion_se_rechaza(cliente, creacion):
    """Crearlo igual daría el peor resultado: un ticket que el cliente que lo
    pidió no puede leer, y que sí ve otro cliente."""
    creacion["org_del_email"] = 99

    respuesta = cliente.post(
        "/api/v1/tickets", json=TICKET_NUEVO, headers=cabeceras(CLAVE_A)
    )
    assert respuesta.status_code == 400
    assert "otra organización" in respuesta.json()["mensaje"]
    assert creacion["asignaciones"] == []


def test_cliente_interno_no_toca_organizaciones(cliente, creacion):
    """org_id 0 no debe mover a nadie de organización."""
    respuesta = cliente.post(
        "/api/v1/tickets", json=TICKET_NUEVO, headers=cabeceras(CLAVE)
    )
    assert respuesta.status_code == 201
    assert creacion["asignaciones"] == []
