"""Devolver el contenido de los adjuntos en el detalle del ticket.

osTicket trocea los archivos en ost_file_chunk y los lee en orden de
chunk_id (AttachmentChunkedData, upload/include/class.file.php:930). Lo que
se prueba acá es que se reensamblen bien y que un ticket con imágenes no
tumbe el proceso ni deje al consumidor sin saber que había una.
"""

import base64
from datetime import datetime

import pytest

from app.core.config import settings
from app.services import ticket_service
from tests.conftest import CABECERAS

IMAGEN = b"\xff\xd8\xff\xe0" + b"contenido binario de la foto" * 4

TICKET = {
    "ticket_id": 17, "numero": "483920", "asunto": "Falla", "estado": "Open",
    "state": "open", "abierto": True, "prioridad": "Normal",
    "departamento": "Support", "tema": "Soporte", "agente_asignado": None,
    "usuario": {"id": 5, "nombre": "Cliente", "email": "c@ejemplo.cl"},
    "creado": datetime(2026, 8, 1, 10, 0), "actualizado": None, "cerrado": None,
    "vencimiento": None, "atrasado": False, "respondido": False,
    "fuente": "API", "ip_origen": "127.0.0.1", "ultima_respuesta": None,
    "ultimo_mensaje": None, "_thread_id": 3,
}


def _adjunto(**kw):
    base = {"id": 1, "file_id": 90, "nombre": "foto.jpg", "tipo_mime": "image/jpeg",
            "tamano": len(IMAGEN), "inline": False, "cid": None, "_bk": "D"}
    base.update(kw)
    return base


@pytest.fixture
def repo(monkeypatch):
    class Falso:
        adjuntos = [_adjunto()]
        contenidos = {90: IMAGEN}
        pedidos = []

        def obtener_ticket(self, conexion, numero, org_id=0):
            return dict(TICKET)

        def obtener_hilo(self, conexion, thread_id, incluir_notas):
            # Una imagen en el mensaje inicial y otra en una respuesta: es el
            # caso que importa, porque el contenido se resuelve para todo el
            # hilo de una vez.
            return [
                {"id": 1, "tipo": "mensaje", "autor": "Cliente", "titulo": "Falla",
                 "cuerpo": "<p>Adjunto</p>", "formato": "html",
                 "creado": TICKET["creado"],
                 "adjuntos": [dict(a) for a in self.adjuntos]},
                {"id": 2, "tipo": "respuesta", "autor": "Agente", "titulo": "Re",
                 "cuerpo": "<p>Recibido</p>", "formato": "html",
                 "creado": TICKET["creado"],
                 "adjuntos": [dict(a, id=2, file_id=91) for a in self.adjuntos]},
            ]

        def contenido_de_archivos(self, conexion, ids):
            self.pedidos.append(list(ids))
            return {i: self.contenidos.get(i, IMAGEN) for i in ids}

    falso = Falso()
    monkeypatch.setattr(ticket_service, "ticket_repo", falso)
    return falso


def test_sin_pedirlo_no_viene_el_contenido(cliente, repo):
    """Es lo que evita que cada consulta de detalle arrastre megabytes."""
    cuerpo = cliente.get("/api/v1/tickets/483920", headers=CABECERAS).json()
    adjunto = cuerpo["mensajes"][0]["adjuntos"][0]
    assert adjunto["contenido_base64"] is None
    assert adjunto["nombre"] == "foto.jpg"


def test_el_contenido_llega_en_base64_y_es_el_original(cliente, repo):
    cuerpo = cliente.get(
        "/api/v1/tickets/483920?incluir_contenido=true", headers=CABECERAS
    ).json()
    adjunto = cuerpo["mensajes"][0]["adjuntos"][0]
    assert base64.b64decode(adjunto["contenido_base64"]) == IMAGEN


def test_tambien_en_las_respuestas_no_solo_en_el_mensaje(cliente, repo):
    cuerpo = cliente.get(
        "/api/v1/tickets/483920?incluir_contenido=true", headers=CABECERAS
    ).json()
    respuesta = [m for m in cuerpo["mensajes"] if m["tipo"] == "respuesta"][0]
    assert base64.b64decode(respuesta["adjuntos"][0]["contenido_base64"]) == IMAGEN


def test_todo_el_hilo_se_resuelve_en_una_sola_consulta(cliente, repo):
    """Un hilo largo con capturas haría decenas de viajes a la base."""
    cliente.get("/api/v1/tickets/483920?incluir_contenido=true", headers=CABECERAS)
    assert len(repo.pedidos) == 1
    assert sorted(repo.pedidos[0]) == [90, 91]


def test_los_campos_internos_no_se_publican(cliente, repo):
    """_bk y file_id son de uso interno del repositorio."""
    for url in ["/api/v1/tickets/483920",
                "/api/v1/tickets/483920?incluir_contenido=true"]:
        cuerpo = cliente.get(url, headers=CABECERAS).json()
        adjunto = cuerpo["mensajes"][0]["adjuntos"][0]
        assert "_bk" not in adjunto and "file_id" not in adjunto


# ─────────────────────────────────────────────────────────────
# Imágenes incrustadas en el cuerpo
# ─────────────────────────────────────────────────────────────

def test_las_imagenes_incrustadas_vienen_con_su_cid(cliente, repo):
    """Los tickets creados desde el portal web llevan la foto dentro del HTML
    como <img src="cid:CLAVE">. Antes se excluían y no aparecían por ningún
    lado en la respuesta."""
    repo.adjuntos = [_adjunto(inline=True, cid="8u5pbmprkplgc3rwlkl4zqatcxxan2d4")]

    cuerpo = cliente.get(
        "/api/v1/tickets/483920?incluir_contenido=true", headers=CABECERAS
    ).json()
    adjunto = cuerpo["mensajes"][0]["adjuntos"][0]
    assert adjunto["inline"] is True
    assert adjunto["cid"] == "8u5pbmprkplgc3rwlkl4zqatcxxan2d4"
    assert base64.b64decode(adjunto["contenido_base64"]) == IMAGEN


# ─────────────────────────────────────────────────────────────
# Lo que no se puede entregar se dice, no se omite
# ─────────────────────────────────────────────────────────────

def test_archivo_en_otro_backend_explica_por_que_no_viene(cliente, repo):
    """osTicket puede guardar los archivos fuera de la base (ost_file.bk)."""
    repo.adjuntos = [_adjunto(_bk="F")]

    cuerpo = cliente.get(
        "/api/v1/tickets/483920?incluir_contenido=true", headers=CABECERAS
    ).json()
    adjunto = cuerpo["mensajes"][0]["adjuntos"][0]
    assert adjunto["contenido_base64"] is None
    assert "almacenamiento" in adjunto["error"]
    # El adjunto se sigue listando: omitirlo haría creer que no había imagen.
    assert adjunto["nombre"] == "foto.jpg"


def test_se_respeta_el_tope_de_tamano(cliente, repo):
    enorme = (settings.ADJUNTOS_DESCARGA_MAX_MB + 1) * 1024 * 1024
    repo.adjuntos = [_adjunto(tamano=enorme)]

    cuerpo = cliente.get(
        "/api/v1/tickets/483920?incluir_contenido=true", headers=CABECERAS
    ).json()
    adjunto = cuerpo["mensajes"][0]["adjuntos"][0]
    assert adjunto["contenido_base64"] is None
    assert "MB" in adjunto["error"]


def test_archivo_sin_contenido_en_la_base(cliente, repo):
    repo.contenidos = {}

    class SinDatos(type(repo)):
        pass

    repo.contenido_de_archivos = lambda conexion, ids: {}
    cuerpo = cliente.get(
        "/api/v1/tickets/483920?incluir_contenido=true", headers=CABECERAS
    ).json()
    assert cuerpo["mensajes"][0]["adjuntos"][0]["error"] == (
        "El archivo no tiene contenido en la base"
    )


# ─────────────────────────────────────────────────────────────
# Reensamblado de los trozos
# ─────────────────────────────────────────────────────────────
# osTicket guarda cada archivo partido en ost_file_chunk. Si se unen fuera de
# orden, el archivo sale corrupto y no se nota hasta que alguien intenta
# abrir la imagen.

def test_los_trozos_se_unen_en_orden_de_chunk_id():
    from app.repositories import ticket_repo

    partes = [b"\xff\xd8\xff", b"medio-binario\x00\x01", b"\xff\xd9"]

    class Fila:
        def __init__(self, file_id, filedata):
            self.file_id, self.filedata = file_id, filedata

    class Cx:
        def execute(self, stmt, params=None):
            assert "ORDER BY file_id, chunk_id" in str(stmt)
            self._filas = [Fila(90, p) for p in partes]
            return self
        def all(self): return self._filas

    assert ticket_repo.contenido_de_archivos(Cx(), [90])[90] == b"".join(partes)


def test_los_bytes_binarios_sobreviven_intactos():
    """El motivo de unirlos en Python y no con GROUP_CONCAT: ese pasa por el
    charset de la conexión y corrompe el binario."""
    from app.repositories import ticket_repo

    crudo = bytes(range(256))

    class Fila:
        file_id, filedata = 90, crudo

    class Cx:
        def execute(self, stmt, params=None): return self
        def all(self): return [Fila()]

    assert ticket_repo.contenido_de_archivos(Cx(), [90])[90] == crudo


def test_varios_archivos_no_se_mezclan():
    from app.repositories import ticket_repo

    class Fila:
        def __init__(self, file_id, filedata):
            self.file_id, self.filedata = file_id, filedata

    class Cx:
        def execute(self, stmt, params=None):
            self._filas = [Fila(90, b"aa"), Fila(90, b"bb"),
                           Fila(91, b"xx"), Fila(91, b"yy")]
            return self
        def all(self): return self._filas

    resultado = ticket_repo.contenido_de_archivos(Cx(), [90, 91])
    assert resultado == {90: b"aabb", 91: b"xxyy"}
