"""Lecturas contra la base de osTicket.

osTicket v1.18 no expone ninguna API de lectura (su dispatcher solo publica
POST /api/tickets.* y POST /api/tasks/cron), así que consultar tickets pasa
necesariamente por SQL. Todo lo de este módulo es de solo lectura.

El prefijo de tabla se interpola una única vez, al construir las constantes de
abajo, y nunca con datos de request. Los valores que vienen del usuario van
siempre como parámetros ligados.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from app.core.config import settings
# ORG_TODAS se define en core.security, que es donde vive el concepto de
# cliente: duplicar la constante acá dejaría dos verdades que mantener en
# sincronía para un límite de seguridad.
from app.core.security import ORG_TODAS
from app.schemas.ticket import FiltroEstado

P = settings.prefijo

# Cabecera común de ticket. `cdata` guarda los campos del formulario dinámico
# (asunto y prioridad viven ahí, no en ost_ticket).
_SELECT_TICKET = f"""
    SELECT
        t.ticket_id,
        t.number,
        t.created,
        t.updated,
        t.closed,
        t.duedate,
        t.isoverdue,
        t.isanswered,
        t.source,
        t.ip_address,
        c.subject,
        s.name  AS estado,
        s.state AS state,
        p.priority_desc AS prioridad,
        d.name  AS departamento,
        ht.topic AS tema,
        NULLIF(TRIM(CONCAT_WS(' ', st.firstname, st.lastname)), '') AS agente,
        u.id    AS usuario_id,
        u.name  AS usuario_nombre,
        ue.address AS usuario_email,
        th.id   AS thread_id,
        th.lastresponse,
        th.lastmessage
    FROM {P}ticket t
    LEFT JOIN {P}ticket__cdata   c  ON c.ticket_id = t.ticket_id
    LEFT JOIN {P}ticket_status   s  ON s.id = t.status_id
    -- cdata.priority es mediumtext y guarda el id de la prioridad. El CAST
    -- hace explícita la conversión: comparar texto con un entero deja que
    -- MySQL convierta a su criterio, y '' se volvería 0 con warning.
    LEFT JOIN {P}ticket_priority p  ON p.priority_id = CAST(NULLIF(c.priority, '') AS UNSIGNED)
    LEFT JOIN {P}department      d  ON d.id = t.dept_id
    LEFT JOIN {P}help_topic      ht ON ht.topic_id = t.topic_id
    LEFT JOIN {P}staff           st ON st.staff_id = t.staff_id
    LEFT JOIN {P}user            u  ON u.id = t.user_id
    -- osTicket deja ost_ticket.user_email_id = 0 en los tickets que crea,
    -- tanto por la API nativa como por el portal web (verificado contra la
    -- base). Solo sirve cuando el ticket apunta a un email concreto de un
    -- usuario con varios; si no, el bueno es el email por defecto del usuario.
    LEFT JOIN {P}user_email      ue ON ue.id = COALESCE(NULLIF(t.user_email_id, 0),
                                                        u.default_email_id)
    LEFT JOIN {P}thread          th ON th.object_id = t.ticket_id AND th.object_type = 'T'
"""

# Los tickets marcados para borrar no son visibles ni en las colas de agentes
# ni en el portal del cliente: exponerlos por la API sería incoherente.
_EXCLUIR_BORRADOS = "COALESCE(s.state, '') <> 'deleted'"

def _filtro_org(org_id: int, alias: str = "u"):
    """Condición y parámetro que limitan una consulta a una organización.

    Devuelve (condiciones, params) para mezclar con el resto del WHERE. Con
    ORG_TODAS no filtra nada: es el caso del consumidor interno.

    Este es el mecanismo de aislamiento entre clientes de la API, así que va
    en un solo lugar: cada consulta que devuelva tickets tiene que pasar por
    acá, y una que se olvide de llamarlo se los muestra todos.
    """
    if org_id == ORG_TODAS:
        return [], {}
    return [f"{alias}.org_id = :org_id"], {"org_id": org_id}


def _fila_a_resumen(fila: Any) -> Dict[str, Any]:
    """Traduce una fila de osTicket a la forma que publica la API."""
    return {
        "ticket_id": fila.ticket_id,
        "numero": fila.number or "",
        "asunto": fila.subject,
        "estado": fila.estado,
        "state": fila.state,
        "abierto": fila.state == "open",
        "prioridad": fila.prioridad,
        "departamento": fila.departamento,
        "tema": fila.tema,
        "agente_asignado": fila.agente,
        "usuario": {
            "id": fila.usuario_id,
            "nombre": fila.usuario_nombre,
            "email": fila.usuario_email,
        },
        "creado": fila.created,
        "actualizado": fila.updated,
        "cerrado": fila.closed,
        "vencimiento": fila.duedate,
        "atrasado": bool(fila.isoverdue),
        "respondido": bool(fila.isanswered),
    }


def buscar_usuario_por_email(conexion: Connection, email: str,
                             org_id: int = ORG_TODAS) -> Optional[Dict[str, Any]]:
    """Resuelve el usuario dueño de los tickets a partir de su email.

    Un usuario de osTicket puede tener varios emails (ost_user_email), así
    que se busca por dirección y se devuelve el user_id, que es el que
    referencia ost_ticket.user_id.

    El filtro de organización va acá dentro y no en el servicio: así un email
    de otra organización es indistinguible de uno que no existe. Devolverlo y
    filtrar después confirmaría que esa persona está en el helpdesk.
    """
    condiciones, params = _filtro_org(org_id)
    where = " AND ".join(["ue.address = :email", *condiciones])

    fila = conexion.execute(
        text(f"""
            SELECT u.id, u.name, ue.address
            FROM {P}user_email ue
            JOIN {P}user u ON u.id = ue.user_id
            WHERE {where}
            LIMIT 1
        """),
        {"email": email, **params},
    ).first()
    if not fila:
        return None
    return {"id": fila.id, "nombre": fila.name, "email": fila.address}


def _condiciones_estado(estado: FiltroEstado, estado_nombre: Optional[str]
                        ) -> Tuple[List[str], Dict[str, Any]]:
    condiciones: List[str] = [_EXCLUIR_BORRADOS]
    params: Dict[str, Any] = {}

    if estado_nombre:
        # Un nombre concreto ("Resolved", "En espera") manda sobre el filtro
        # de alto nivel: es más específico.
        condiciones.append("s.name = :estado_nombre")
        params["estado_nombre"] = estado_nombre
    elif estado == FiltroEstado.ABIERTOS:
        condiciones.append("s.state = 'open'")
    elif estado == FiltroEstado.CERRADOS:
        condiciones.append("s.state = 'closed'")
    # FiltroEstado.TODOS no agrega nada (salvo la exclusión de borrados).

    return condiciones, params


def listar_tickets_de_usuario(
    conexion: Connection,
    user_id: int,
    estado: FiltroEstado = FiltroEstado.ABIERTOS,
    estado_nombre: Optional[str] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    pagina: int = 1,
    tamano: int = 25,
    org_id: int = ORG_TODAS,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Tickets creados por un usuario. Devuelve (total, página de tickets)."""
    condiciones, params = _condiciones_estado(estado, estado_nombre)
    condiciones.append("t.user_id = :user_id")
    params["user_id"] = user_id

    # Redundante con el filtro de buscar_usuario_por_email, que es por donde
    # se obtiene el user_id: si ese usuario no fuera de la organización, acá
    # no se habría llegado. Se repite igual porque es un límite de seguridad
    # y no depende de que quien llame haya resuelto el usuario como
    # corresponde.
    cond_org, params_org = _filtro_org(org_id)
    condiciones.extend(cond_org)
    params.update(params_org)

    if desde:
        condiciones.append("t.created >= :desde")
        params["desde"] = desde
    if hasta:
        # < día siguiente en vez de <= :hasta, porque created es datetime y
        # un <= con una fecha pelada deja fuera todo lo del propio día.
        condiciones.append("t.created < DATE_ADD(:hasta, INTERVAL 1 DAY)")
        params["hasta"] = hasta

    where = " WHERE " + " AND ".join(condiciones)

    total = conexion.execute(
        text(f"""
            SELECT COUNT(*)
            FROM {P}ticket t
            LEFT JOIN {P}ticket_status s ON s.id = t.status_id
            -- ost_user no hace falta para contar, pero el WHERE lo comparte
            -- con la consulta de la página y ese lleva el filtro por
            -- organización (u.org_id). Sin este join, un cliente con
            -- aislamiento recibiría "Unknown column 'u.org_id'".
            LEFT JOIN {P}user u ON u.id = t.user_id
            {where}
        """),
        params,
    ).scalar_one()

    if total == 0:
        return 0, []

    filas = conexion.execute(
        text(f"{_SELECT_TICKET} {where} ORDER BY t.created DESC LIMIT :limite OFFSET :salto"),
        {**params, "limite": tamano, "salto": (pagina - 1) * tamano},
    ).all()

    return total, [_fila_a_resumen(fila) for fila in filas]


def obtener_ticket(conexion: Connection, numero: str,
                   org_id: int = ORG_TODAS) -> Optional[Dict[str, Any]]:
    """Cabecera de un ticket por su número (el que ve el cliente).

    Un ticket de otra organización se comporta como uno inexistente: el
    servicio lo traduce a 404. Un 403 confirmaría que ese número existe, y
    los números de osTicket son enumerables.
    """
    condiciones, params = _filtro_org(org_id)
    where = " AND ".join(["t.number = :numero", _EXCLUIR_BORRADOS, *condiciones])

    fila = conexion.execute(
        text(f"{_SELECT_TICKET} WHERE {where} LIMIT 1"),
        {"numero": numero, **params},
    ).first()
    if not fila:
        return None

    detalle = _fila_a_resumen(fila)
    detalle.update({
        "fuente": fila.source,
        "ip_origen": fila.ip_address or None,
        "ultima_respuesta": fila.lastresponse,
        "ultimo_mensaje": fila.lastmessage,
        "_thread_id": fila.thread_id,
    })
    return detalle


_TIPOS_ENTRADA = {"M": "mensaje", "R": "respuesta", "N": "nota"}


def obtener_hilo(conexion: Connection, thread_id: int,
                 incluir_notas: bool = False) -> List[Dict[str, Any]]:
    """Mensajes, respuestas y (opcionalmente) notas internas del ticket."""
    if not thread_id:
        return []

    tipos = ["M", "R", "N"] if incluir_notas else ["M", "R"]
    # expanding=True deja que SQLAlchemy arme el IN con tantos placeholders
    # como elementos tenga la lista, en vez de ligar la lista entera como un
    # único valor.
    consulta = text(f"""
        SELECT e.id, e.type, e.poster, e.title, e.body, e.format, e.created
        FROM {P}thread_entry e
        WHERE e.thread_id = :thread_id
          AND e.type IN :tipos
        ORDER BY e.created ASC, e.id ASC
    """).bindparams(bindparam("tipos", expanding=True))

    filas = conexion.execute(
        consulta, {"thread_id": thread_id, "tipos": tipos}
    ).all()

    if not filas:
        return []

    adjuntos = _adjuntos_por_entrada(conexion, [fila.id for fila in filas])

    return [
        {
            "id": fila.id,
            "tipo": _TIPOS_ENTRADA.get(fila.type, "mensaje"),
            "autor": fila.poster or None,
            "titulo": fila.title,
            "cuerpo": fila.body or "",
            "formato": fila.format,
            "creado": fila.created,
            "adjuntos": adjuntos.get(fila.id, []),
        }
        for fila in filas
    ]


def _adjuntos_por_entrada(conexion: Connection,
                          ids_entrada: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Adjuntos de las entradas del hilo, agrupados por entrada.

    En ost_attachment, las entradas del hilo se identifican con type='H'
    (ver upload/include/class.thread.php:1237); object_id es el id de la
    entrada.

    Se incluyen también los `inline`, que antes se descartaban. Son las
    imágenes incrustadas en el cuerpo: cuando el ticket se crea desde el
    portal web, la foto va como <img src="cid:LA-CLAVE"> dentro del HTML y
    su fila lleva inline = 1. Excluirlas hacía que esas imágenes no
    aparecieran por ninguna parte en la respuesta de la API, ni como adjunto
    ni de otra forma. Se marcan con `inline` y se publica su `cid` para que
    el consumidor pueda cruzarlas con el src del cuerpo.
    """
    if not ids_entrada:
        return {}

    filas = conexion.execute(
        text(f"""
            SELECT a.object_id, a.id, a.file_id, a.inline,
                   COALESCE(a.name, f.name) AS nombre,
                   f.type AS tipo_mime, f.size AS tamano,
                   f.`key` AS clave, f.bk
            FROM {P}attachment a
            LEFT JOIN {P}file f ON f.id = a.file_id
            WHERE a.type = 'H'
              AND a.object_id IN :ids
        """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids_entrada},
    ).all()

    agrupados: Dict[int, List[Dict[str, Any]]] = {}
    for fila in filas:
        agrupados.setdefault(fila.object_id, []).append({
            "id": fila.id,
            "file_id": fila.file_id,
            "nombre": fila.nombre,
            "tipo_mime": fila.tipo_mime,
            "tamano": fila.tamano,
            "inline": bool(fila.inline),
            # El cuerpo referencia las imágenes incrustadas como
            # <img src="cid:LA-CLAVE">, y esa clave es ost_file.key. Se
            # publica para poder emparejarlas sin tener que parsear el HTML
            # a ciegas.
            "cid": fila.clave if fila.inline else None,
            # Backend de almacenamiento (ost_file.bk). 'D' = el contenido
            # está en ost_file_chunk, que es lo único que se puede leer
            # desde acá; con otro backend el archivo vive en disco o en un
            # servicio externo, fuera del alcance de esta API.
            "_bk": fila.bk,
        })
    return agrupados


def contenido_de_archivos(conexion: Connection,
                          ids_archivo: List[int]) -> Dict[int, bytes]:
    """Contenido binario de los archivos, leído de ost_file_chunk.

    osTicket trocea los archivos y los lee en orden de chunk_id
    (AttachmentChunkedData, upload/include/class.file.php:930). Respetar ese
    orden es obligatorio: concatenar los trozos como vengan da un archivo
    corrupto, y es un fallo que no se nota hasta que alguien intenta abrir
    la imagen.

    Los trozos se traen como filas y se unen en Python, y NO con un
    GROUP_CONCAT en la base. GROUP_CONCAT devuelve un tipo de texto con el
    charset de la conexión (acá utf8mb4): pasar bytes de un JPEG por esa
    conversión los corrompe. Además tiene el tope group_concat_max_len, de
    1024 bytes por defecto, que trunca en silencio.

    Solo aplica a los archivos con bk = 'D' (los guardados en la base), que
    es el backend por defecto de osTicket.
    """
    if not ids_archivo:
        return {}

    filas = conexion.execute(
        text(f"""
            SELECT file_id, filedata
            FROM {P}file_chunk
            WHERE file_id IN :ids
            ORDER BY file_id, chunk_id
        """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids_archivo},
    ).all()

    trozos: Dict[int, List[bytes]] = {}
    for fila in filas:
        datos = fila.filedata
        if datos is None:
            continue
        if not isinstance(datos, (bytes, bytearray)):
            # Si el driver devolviera texto, se vuelve a bytes sin pasar por
            # una decodificación que ya habría alterado el binario.
            datos = str(datos).encode("latin-1", errors="replace")
        trozos.setdefault(fila.file_id, []).append(bytes(datos))

    return {file_id: b"".join(partes) for file_id, partes in trozos.items()}


def obtener_estado(conexion: Connection, numero: str,
                   org_id: int = ORG_TODAS) -> Optional[Dict[str, Any]]:
    """Consulta mínima de estado: no toca cdata ni el hilo.

    A diferencia de _SELECT_TICKET, esta consulta no necesitaba ost_user para
    nada; se joinea solo para poder filtrar por organización. Sin ese join,
    este endpoint sería el agujero por donde un cliente confirmaría el estado
    de los tickets de otro.
    """
    condiciones, params = _filtro_org(org_id)
    where = " AND ".join([
        "t.number = :numero",
        "COALESCE(s.state, '') <> 'deleted'",
        *condiciones,
    ])

    fila = conexion.execute(
        text(f"""
            SELECT t.number, t.created, t.updated, t.closed, t.duedate, t.isoverdue,
                   s.name AS estado, s.state AS state,
                   NULLIF(TRIM(CONCAT_WS(' ', st.firstname, st.lastname)), '') AS agente
            FROM {P}ticket t
            LEFT JOIN {P}ticket_status s ON s.id = t.status_id
            LEFT JOIN {P}staff st ON st.staff_id = t.staff_id
            LEFT JOIN {P}user u ON u.id = t.user_id
            WHERE {where}
            LIMIT 1
        """),
        {"numero": numero, **params},
    ).first()
    if not fila:
        return None

    return {
        "numero": fila.number or "",
        "estado": fila.estado,
        "state": fila.state,
        "abierto": fila.state == "open",
        "agente_asignado": fila.agente,
        "creado": fila.created,
        "actualizado": fila.updated,
        "cerrado": fila.closed,
        "vencimiento": fila.duedate,
        "atrasado": bool(fila.isoverdue),
    }


def estado_api_key(conexion: Connection, apikey: str) -> Optional[Dict[str, Any]]:
    """Diagnostica la API Key nativa contra ost_api_key.

    osTicket la busca con `WHERE apikey=... AND ipaddr=...`
    (upload/include/class.api.php:104), coincidencia EXACTA en las dos
    columnas, y cuando no encuentra fila contesta 401 "Valid API key
    required" sin decir cuál de las dos falló. Como acá sí se puede leer la
    tabla, se busca solo por la clave: así se distingue "la clave no existe"
    de "existe, pero registrada para otra IP", que son arreglos distintos.

    Devuelve None si la clave no está en la tabla.
    """
    if not apikey:
        return None

    fila = conexion.execute(
        text(f"""
            SELECT ipaddr, isactive, can_create_tickets
            FROM {P}api_key
            WHERE apikey = :apikey
            LIMIT 1
        """),
        {"apikey": apikey},
    ).first()
    if not fila:
        return None
    return {
        "ip_registrada": fila.ipaddr,
        "activa": bool(fila.isactive),
        "puede_crear": bool(fila.can_create_tickets),
    }


def adjuntos_habilitados(conexion: Connection) -> bool:
    """¿osTicket aceptará los adjuntos que le mande la API?

    Vale la pena comprobarlo porque el fallo es SILENCIOSO: en
    upload/include/api.tickets.php:85, si el campo "message" del formulario
    de tickets no tiene adjuntos habilitados, osTicket hace
    `$data['attachments'] = array()` y sigue adelante. El ticket se crea con
    201, la API lo da por bueno y las imágenes simplemente no están en
    ninguna parte. Nadie se entera hasta que un agente abre el ticket.

    El valor sale de ost_config.allow_attachments, que es el que usa por
    defecto ese campo del formulario (class.forms.php: 'default' =>
    $cfg->allowAttachments()). Si la fila no existe, osTicket lo toma como
    desactivado.
    """
    valor = conexion.execute(
        text(f"""
            SELECT value FROM {P}config
            WHERE namespace = 'core' AND `key` = 'allow_attachments'
            LIMIT 1
        """)
    ).scalar()
    return str(valor or "0").strip() not in ("", "0")


def listar_temas(conexion: Connection) -> List[Dict[str, Any]]:
    filas = conexion.execute(
        text(f"""
            SELECT topic_id AS id, topic AS nombre
            FROM {P}help_topic
            WHERE ispublic = 1 and flags <> 0
            ORDER BY sort, topic
        """)
    ).all()
    return [{"id": f.id, "nombre": f.nombre} for f in filas]


def listar_estados(conexion: Connection) -> List[Dict[str, Any]]:
    filas = conexion.execute(
        text(f"""
            SELECT id, name AS nombre, state
            FROM {P}ticket_status
            WHERE state <> 'deleted' 
            ORDER BY sort, id
        """)
    ).all()
    return [{"id": f.id, "nombre": f.nombre, "state": f.state} for f in filas]


def listar_prioridades(conexion: Connection) -> List[Dict[str, Any]]:
    filas = conexion.execute(
        text(f"""
            SELECT priority_id AS id, priority_desc AS nombre
            FROM {P}ticket_priority
            WHERE ispublic = 1
            ORDER BY priority_urgency
        """)
    ).all()
    return [{"id": f.id, "nombre": f.nombre} for f in filas]
