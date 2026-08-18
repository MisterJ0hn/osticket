"""Adjuntos: límites de tamaño y qué pasa cuando la API nativa falla.

El caso que motiva estos tests: el CRM manda tickets con imágenes, osTicket
no los procesa y la API cae al fallback SQL, que descarta los adjuntos. El
ticket queda creado pero sin las fotos, y el mensaje de respuesta no decía
por qué.
"""

import base64
from contextlib import contextmanager

import pytest

from app.core.config import settings
from app.core.exceptions import OsticketApiError
from app.schemas.ticket import Adjunto, CrearTicketRequest
from app.services import osticket_client, ticket_service
from tests.conftest import CABECERAS


def _adjunto(mb: float, nombre="foto.jpg") -> dict:
    contenido = base64.b64encode(b"x" * int(mb * 1024 * 1024)).decode()
    return {"nombre": nombre, "contenido_base64": contenido, "tipo_mime": "image/jpeg"}


TICKET = {
    "email": "cliente@ejemplo.cl",
    "asunto": "Falla con foto",
    "mensaje": "Adjunto la pantalla",
}


# ─────────────────────────────────────────────────────────────
# Medición del tamaño
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bytes_reales", [1, 2, 3, 1024, 100_000])
def test_tamano_estimado_coincide_con_el_real(bytes_reales):
    """Se estima desde el largo del base64 para no duplicar el archivo en
    memoria solo para medirlo: la estimación tiene que ser exacta."""
    crudo = b"x" * bytes_reales
    adjunto = Adjunto(nombre="f.bin", contenido_base64=base64.b64encode(crudo).decode())
    assert adjunto.tamano_bytes == bytes_reales


# ─────────────────────────────────────────────────────────────
# Límite de tamaño
# ─────────────────────────────────────────────────────────────

def test_adjunto_demasiado_grande_se_rechaza_con_422(cliente):
    """Mejor un error claro que un ticket creado a medias sin las imágenes."""
    grande = _adjunto(settings.ADJUNTOS_MAX_MB + 2)
    respuesta = cliente.post(
        "/api/v1/tickets", json={**TICKET, "adjuntos": [grande]}, headers=CABECERAS
    )
    assert respuesta.status_code == 422
    assert "MB" in respuesta.text


def test_el_limite_es_sobre_la_suma_de_los_adjuntos(cliente):
    """Varias fotos medianas también desbordan a PHP: el límite es el total."""
    mitad = settings.ADJUNTOS_MAX_MB / 2 + 1
    respuesta = cliente.post(
        "/api/v1/tickets",
        json={**TICKET, "adjuntos": [_adjunto(mitad, "a.jpg"), _adjunto(mitad, "b.jpg")]},
        headers=CABECERAS,
    )
    assert respuesta.status_code == 422


def test_adjunto_dentro_del_limite_pasa(cliente, monkeypatch):
    monkeypatch.setattr(osticket_client, "crear_ticket", lambda payload: "483920")
    monkeypatch.setattr(ticket_service, "_resolver_ticket_id", lambda numero: 17)

    respuesta = cliente.post(
        "/api/v1/tickets", json={**TICKET, "adjuntos": [_adjunto(0.5)]}, headers=CABECERAS
    )
    assert respuesta.status_code == 201
    assert respuesta.json()["origen"] == "nativa"


# ─────────────────────────────────────────────────────────────
# Timeout: el que provoca tickets duplicados
# ─────────────────────────────────────────────────────────────

def test_con_adjuntos_se_usa_el_timeout_largo(monkeypatch):
    """Si rendimos antes que osTicket, él puede terminar creando el ticket
    mientras nosotros ya lo creamos por SQL: quedarían dos."""
    usados = []

    class RespuestaFalsa:
        status_code = 201
        text = "483920"

    def post_falso(url, json=None, headers=None, timeout=None):
        usados.append(timeout)
        return RespuestaFalsa()

    monkeypatch.setattr(osticket_client.httpx, "post", post_falso)
    monkeypatch.setattr(settings, "OSTICKET_API_KEY", "clave", raising=False)

    osticket_client.crear_ticket({"email": "a@b.cl", "attachments": [{"f.jpg": "data:..."}]})
    osticket_client.crear_ticket({"email": "a@b.cl"})

    assert usados[0] == settings.OSTICKET_TIMEOUT_ADJUNTOS
    assert usados[1] == settings.OSTICKET_TIMEOUT
    assert usados[0] > usados[1]


# ─────────────────────────────────────────────────────────────
# El mensaje del modo degradado
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def fallback(monkeypatch):
    @contextmanager
    def transaccion_falsa():
        yield None

    monkeypatch.setattr(ticket_service.database, "transaccion", transaccion_falsa)
    monkeypatch.setattr(ticket_service, "_resolver_ticket_id", lambda numero: 17)
    monkeypatch.setattr(
        ticket_service.ticket_write_repo, "crear_ticket",
        lambda conexion, **kw: {"numero": "483920", "ticket_id": 17},
    )

    def revienta(payload):
        raise OsticketApiError(
            "osTicket rechazó la creación (HTTP 500): PHP Fatal error: memoria agotada",
            status_code=500, es_transporte=True,
        )

    monkeypatch.setattr(osticket_client, "crear_ticket", revienta)


def test_el_mensaje_degradado_dice_la_causa(cliente, fallback):
    """Sin la causa, quien recibe la respuesta no puede hacer nada con ella
    más que pedirle los logs a otra persona."""
    respuesta = cliente.post(
        "/api/v1/tickets", json={**TICKET, "adjuntos": [_adjunto(0.2)]}, headers=CABECERAS
    )
    assert respuesta.status_code == 201
    mensaje = respuesta.json()["mensaje"]
    assert "HTTP 500" in mensaje and "memoria agotada" in mensaje


def test_el_mensaje_avisa_de_los_adjuntos_perdidos(cliente, fallback):
    respuesta = cliente.post(
        "/api/v1/tickets",
        json={**TICKET, "adjuntos": [_adjunto(0.2, "a.jpg"), _adjunto(0.2, "b.jpg")]},
        headers=CABECERAS,
    )
    mensaje = respuesta.json()["mensaje"]
    assert "2 adjuntos" in mensaje and "a mano" in mensaje


def test_sin_adjuntos_no_habla_de_adjuntos(cliente, fallback):
    respuesta = cliente.post("/api/v1/tickets", json=TICKET, headers=CABECERAS)
    assert "adjunto" not in respuesta.json()["mensaje"].lower()


# ─────────────────────────────────────────────────────────────
# Cuerpo del mensaje: data URI (RFC 2397)
# ─────────────────────────────────────────────────────────────
# El CRM manda el mensaje como "data:text/html;charset=utf-8,<p>...</p>" para
# que llegue con formato. La API nativa lo interpreta sola; el fallback SQL
# escribe directo en la base y tenía que hacerlo a mano — no lo hacía, y el
# prefijo quedaba a la vista del agente dentro del ticket.

from app.core import rfc2397  # noqa: E402


@pytest.mark.parametrize("entrada, texto, formato", [
    ("data:text/html;charset=utf-8,<p>Hola</p>", "<p>Hola</p>", "html"),
    ("data:text/plain;charset=utf-8,Hola", "Hola", "text"),
    ("data:text/html,<p>Sin charset</p>", "<p>Sin charset</p>", "html"),
    ("data:text/html;base64,PHA+SG9sYTwvcD4=", "<p>Hola</p>", "html"),
    # Lo que no es data URI se guarda tal cual, como texto plano.
    ("Mensaje pelado", "Mensaje pelado", "text"),
    # Un "data:" mal formado no debe hacer perder el mensaje.
    ("data:roto-sin-coma", "data:roto-sin-coma", "text"),
])
def test_parseo_del_cuerpo(entrada, texto, formato):
    resultado = rfc2397.parsear_mensaje(entrada)
    assert resultado.texto == texto
    assert resultado.formato == formato


def test_el_prefijo_data_no_llega_a_la_base(monkeypatch):
    """Regresión del caso reportado: el agente veía el data URI crudo."""
    from app.repositories import ticket_write_repo

    escrito = {}

    class CxFalsa:
        def execute(self, stmt, params=None):
            sql = str(stmt)
            if "thread_entry" in sql and "INSERT" in sql:
                escrito.update(params)
            return self
        def first(self): return None
        def scalar(self): return None
        def scalar_one(self): return 0
        @property
        def lastrowid(self): return 1
        @property
        def rowcount(self): return 1

    monkeypatch.setattr(ticket_write_repo, "_resolver_usuario",
                        lambda *a, **k: (5, 7))
    monkeypatch.setattr(ticket_write_repo, "_datos_del_topic", lambda *a, **k: {})
    monkeypatch.setattr(ticket_write_repo, "_estado_por_defecto", lambda *a, **k: 1)
    monkeypatch.setattr(ticket_write_repo, "_generar_numero", lambda *a, **k: "483920")
    monkeypatch.setattr(ticket_write_repo, "_config", lambda *a, **k: "1")

    ticket_write_repo.crear_ticket(
        CxFalsa(), email="a@b.cl", asunto="Prueba",
        mensaje="data:text/html;charset=utf-8,<p>se aumenta limite</p>",
    )

    assert not escrito["cuerpo"].startswith("data:")
    assert escrito["cuerpo"] == "<p>se aumenta limite</p>"
    assert escrito["formato"] == "html"


# ─────────────────────────────────────────────────────────────
# Diagnóstico de la API Key nativa
# ─────────────────────────────────────────────────────────────
# osTicket busca la clave con `WHERE apikey=... AND ipaddr=...` y, si no hay
# fila, contesta 401 "Valid API key required" sin decir cuál de las dos
# falló. Como acá sí se puede leer ost_api_key, se separan los casos.

def test_salud_avisa_si_la_clave_no_existe(cliente_sin_auth, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "verificar_conexion", lambda: True)
    monkeypatch.setattr(main, "verificar_esquema", lambda: None)
    monkeypatch.setattr(main, "_estado_de_adjuntos", lambda: "ok")
    monkeypatch.setattr(main, "_estado_de_api_key",
                        lambda: "la clave configurada NO existe en ost_api_key")

    cuerpo = cliente_sin_auth.get("/salud").json()
    assert "NO existe" in cuerpo["api_nativa"]


def test_salud_publica_la_ip_registrada(cliente_sin_auth, monkeypatch):
    """Cotejar esa IP con la del log de osTicket es lo que resuelve el 401."""
    from app import main
    monkeypatch.setattr(main, "verificar_conexion", lambda: True)
    monkeypatch.setattr(main, "verificar_esquema", lambda: None)
    monkeypatch.setattr(main, "_estado_de_adjuntos", lambda: "ok")
    monkeypatch.setattr(main, "_estado_de_api_key",
                        lambda: "clave ok, registrada para la IP 172.18.0.1")

    assert "172.18.0.1" in cliente_sin_auth.get("/salud").json()["api_nativa"]


@pytest.mark.parametrize("fila, esperado", [
    (None, "NO existe"),
    ({"ip_registrada": "1.2.3.4", "activa": False, "puede_crear": True}, "desactivada"),
    ({"ip_registrada": "1.2.3.4", "activa": True, "puede_crear": False}, "no puede crear"),
    ({"ip_registrada": "1.2.3.4", "activa": True, "puede_crear": True}, "1.2.3.4"),
])
def test_estado_de_la_clave_distingue_cada_causa(monkeypatch, fila, esperado):
    from app import main
    from app.repositories import ticket_repo
    from contextlib import contextmanager

    @contextmanager
    def conexion_falsa():
        yield None

    class EngineFalso:
        connect = staticmethod(conexion_falsa)

    monkeypatch.setattr(main.settings, "OSTICKET_API_KEY", "una-clave", raising=False)
    monkeypatch.setattr(ticket_repo, "estado_api_key", lambda conexion, apikey: fila)
    monkeypatch.setattr("app.core.database.engine", EngineFalso)

    assert esperado in main._estado_de_api_key()
