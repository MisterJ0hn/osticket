"""Creación de tickets escribiendo directo en MySQL (camino de fallback).

Este módulo solo se usa cuando la API nativa de osTicket no responde. Replica
a mano lo mínimo que hace `Ticket::create()`: resolver el usuario, elegir
departamento/SLA/estado/prioridad, generar el número y dejar las cuatro filas
que componen un ticket (ticket, cdata, thread y la entrada del mensaje).

Lo que este camino NO hace, y por eso los tickets quedan marcados con
`source_extra='api-fallback'` para poder auditarlos:

  * no evalúa los filtros de tickets (auto-asignación, rechazo, canned response)
  * no manda auto-respuesta al usuario ni alerta a los agentes
  * no calcula la fecha de vencimiento del SLA
  * no dispara los plugins ni los eventos del hilo

Preferir siempre la API nativa: `OSTICKET_FALLBACK_SQL=false` desactiva esto.
"""

import logging
import re
import secrets
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core import rfc2397
from app.core.config import settings

logger = logging.getLogger(__name__)

P = settings.prefijo

# Cuántas veces se reintenta si el número generado ya existe. ost_ticket.number
# no tiene índice único (solo hay PK en ticket_id), así que la unicidad hay que
# comprobarla a mano, igual que hace Ticket::isTicketNumberUnique().
_MAX_INTENTOS_NUMERO = 10


def _config(conexion: Connection, clave: str, defecto: Optional[str] = None) -> Optional[str]:
    """Lee una opción del namespace 'core' de ost_config."""
    # `key` es palabra reservada en MySQL: va con backticks.
    valor = conexion.execute(
        text(f"SELECT `value` FROM {P}config WHERE namespace = 'core' AND `key` = :clave LIMIT 1"),
        {"clave": clave},
    ).scalar()
    if valor is None or valor == "":
        return defecto
    return valor


def _formatear_numero(formato: str, numero: int, relleno: str = "0") -> str:
    """Coloca `numero` en los grupos de '#' del formato.

    Réplica de Sequence::format() de osTicket: los '#' escapados (\\#) salen
    como almohadilla literal, el número se rellena por la izquierda hasta
    cubrir todos los huecos y el último grupo se queda con los dígitos que
    sobren. Con el formato por defecto ('######') esto es simplemente el
    número a 6 dígitos.
    """
    grupos = list(re.finditer(r"(?<!\\)#+", formato))
    if not grupos:
        return str(numero)

    total = sum(len(g.group(0)) for g in grupos)
    digitos = str(numero).rjust(total, relleno or "0")

    salida = []
    inicio = 0
    consumidos = 0
    for indice, grupo in enumerate(grupos):
        salida.append(formato[inicio:grupo.start()])
        if indice == len(grupos) - 1:
            salida.append(digitos[consumidos:])
        else:
            tamano = len(grupo.group(0))
            salida.append(digitos[consumidos:consumidos + tamano])
            consumidos += tamano
        inicio = grupo.end()
    salida.append(formato[inicio:])

    return "".join(salida).replace("\\#", "#")


def _numero_existe(conexion: Connection, numero: str) -> bool:
    return conexion.execute(
        text(f"SELECT 1 FROM {P}ticket WHERE number = :numero LIMIT 1"),
        {"numero": numero},
    ).first() is not None


def _generar_numero(conexion: Connection, formato_topic: Optional[str] = None) -> str:
    """Genera el número del ticket respetando la configuración del helpdesk.

    Dos modos, como osTicket: con secuencia (ost_sequence, correlativo) o
    aleatorio. Esta instalación usa el segundo: ticket_number_format='######'
    y ticket_sequence_id vacío.
    """
    formato = formato_topic or _config(conexion, "ticket_number_format", "######") or "######"
    sequence_id = _config(conexion, "ticket_sequence_id", "") or ""

    if sequence_id.isdigit() and int(sequence_id) > 0:
        # FOR UPDATE bloquea la fila de la secuencia hasta el commit: sin eso,
        # dos creaciones simultáneas leen el mismo `next` y repiten número.
        fila = conexion.execute(
            text(f"SELECT next, increment, padding FROM {P}sequence WHERE id = :id FOR UPDATE"),
            {"id": int(sequence_id)},
        ).first()
        if fila:
            siguiente = int(fila.next)
            conexion.execute(
                text(f"UPDATE {P}sequence SET next = :nuevo, updated = NOW() WHERE id = :id"),
                {"nuevo": siguiente + int(fila.increment or 1), "id": int(sequence_id)},
            )
            return _formatear_numero(formato, siguiente, fila.padding or "0")
        logger.warning("ticket_sequence_id=%s no existe en %ssequence; se usa aleatorio",
                       sequence_id, P)

    # Aleatorio: osTicket fuerza un mínimo de 6 dígitos (RandomSequence).
    digitos = max(6, sum(len(g) for g in re.findall(r"(?<!\\)#+", formato)) or 6)
    for _ in range(_MAX_INTENTOS_NUMERO):
        candidato = _formatear_numero(
            formato, secrets.randbelow(10 ** digitos - 10 ** (digitos - 1)) + 10 ** (digitos - 1)
        )
        if not _numero_existe(conexion, candidato):
            return candidato

    raise RuntimeError(
        f"No se pudo generar un número de ticket único en {_MAX_INTENTOS_NUMERO} intentos"
    )


def _resolver_usuario(conexion: Connection, email: str, nombre: Optional[str],
                      telefono: Optional[str], org_id: int = 0) -> Tuple[int, int]:
    """Devuelve (user_id, user_email_id), creando el usuario si no existe.

    La auto-creación es el mismo comportamiento que tiene la API nativa: si
    el email no está registrado, osTicket da de alta al usuario con el nombre
    recibido.

    El usuario nuevo se crea con `org_id`, no con 0. Es el detalle del que
    depende que el cliente pueda leer después el ticket que acaba de crear:
    las consultas filtran por ost_user.org_id, así que un usuario sin
    organización queda invisible para todos los clientes con aislamiento.
    """
    fila = conexion.execute(
        text(f"SELECT id, user_id FROM {P}user_email WHERE address = :email LIMIT 1"),
        {"email": email},
    ).first()
    if fila:
        return fila.user_id, fila.id

    nombre_usuario = (nombre or email.split("@")[0]).strip()[:128]

    user_id = conexion.execute(
        text(f"""
            INSERT INTO {P}user (org_id, default_email_id, status, name, created, updated)
            VALUES (:org_id, 0, 0, :nombre, NOW(), NOW())
        """),
        {"nombre": nombre_usuario, "org_id": org_id},
    ).lastrowid

    email_id = conexion.execute(
        text(f"INSERT INTO {P}user_email (user_id, flags, address) VALUES (:uid, 0, :email)"),
        {"uid": user_id, "email": email},
    ).lastrowid

    conexion.execute(
        text(f"UPDATE {P}user SET default_email_id = :eid WHERE id = :uid"),
        {"eid": email_id, "uid": user_id},
    )

    # cdata es lo que lee el panel de agentes para mostrar los datos del
    # usuario; sin esta fila el usuario aparece sin nombre ni correo.
    conexion.execute(
        text(f"""
            INSERT INTO {P}user__cdata (user_id, email, name, phone)
            VALUES (:uid, :email, :nombre, :telefono)
        """),
        {"uid": user_id, "email": email, "nombre": nombre_usuario, "telefono": telefono},
    )

    logger.info("Usuario creado por fallback SQL: %s (id=%s, org_id=%s)",
                email, user_id, org_id)
    return user_id, email_id


def organizacion_del_email(conexion: Connection, email: str) -> Optional[int]:
    """org_id del usuario dueño de ese email, o None si el email no existe.

    Sirve para detectar antes de crear un ticket que el email ya pertenece a
    otra organización.
    """
    fila = conexion.execute(
        text(f"""
            SELECT u.org_id
            FROM {P}user_email ue
            JOIN {P}user u ON u.id = ue.user_id
            WHERE ue.address = :email
            LIMIT 1
        """),
        {"email": email},
    ).first()
    return None if fila is None else int(fila.org_id)


def asignar_organizacion(conexion: Connection, email: str, org_id: int) -> bool:
    """Pone la organización al usuario de ese email, si no tenía ninguna.

    Existe por la API nativa de osTicket: cuando el email no está registrado,
    osTicket crea el usuario por su cuenta y lo deja en org_id = 0 (salvo que
    ost_organization.domain calce con el dominio del correo). Sin este paso,
    el cliente crea el ticket correctamente y después no lo ve, porque las
    consultas filtran por organización.

    Solo toca a los que están en 0: nunca mueve un usuario de una
    organización a otra. Devuelve True si actualizó algo.
    """
    if not org_id:
        return False

    resultado = conexion.execute(
        text(f"""
            UPDATE {P}user u
            JOIN {P}user_email ue ON ue.user_id = u.id
            SET u.org_id = :org_id, u.updated = NOW()
            WHERE ue.address = :email AND u.org_id = 0
        """),
        {"org_id": org_id, "email": email},
    )
    if resultado.rowcount:
        logger.info("Usuario %s asignado a la organización %s", email, org_id)
        return True
    return False


def _datos_del_topic(conexion: Connection, topic_id: Optional[int]) -> Dict[str, Any]:
    if not topic_id:
        return {}
    fila = conexion.execute(
        text(f"""
            SELECT dept_id, sla_id, status_id, priority_id, staff_id, team_id, number_format
            FROM {P}help_topic WHERE topic_id = :id LIMIT 1
        """),
        {"id": topic_id},
    ).first()
    if not fila:
        return {}
    return dict(fila._mapping)


def _estado_por_defecto(conexion: Connection, status_id_topic: int) -> int:
    """Estado inicial: el del tema, si no el configurado, si no el primer 'open'."""
    if status_id_topic:
        return status_id_topic

    configurado = _config(conexion, "default_ticket_status_id", "1")
    if configurado and configurado.isdigit() and int(configurado) > 0:
        return int(configurado)

    return conexion.execute(
        text(f"SELECT id FROM {P}ticket_status WHERE state = 'open' ORDER BY sort, id LIMIT 1")
    ).scalar() or 1


def _entero(valor: Any) -> int:
    try:
        return int(valor or 0)
    except (TypeError, ValueError):
        return 0


def crear_ticket(
    conexion: Connection,
    email: str,
    asunto: str,
    mensaje: str,
    nombre: Optional[str] = None,
    telefono: Optional[str] = None,
    topic_id: Optional[int] = None,
    prioridad_id: Optional[int] = None,
    ip_origen: str = "",
    org_id: int = 0,
) -> Dict[str, Any]:
    """Inserta el ticket completo. Debe ejecutarse dentro de una transacción.

    Todas las fechas se escriben con NOW() de MySQL, no con datetime.now()
    de Python: osTicket hace lo mismo (User::fromVars usa SqlFunction('NOW'))
    y el servidor de base suele estar en UTC mientras el proceso Python corre
    en hora local. Mezclarlos deja los tickets del fallback desfasados varias
    horas respecto de los que crea osTicket.
    """
    user_id, user_email_id = _resolver_usuario(conexion, email, nombre, telefono, org_id)
    topic = _datos_del_topic(conexion, topic_id)

    dept_id = _entero(topic.get("dept_id")) or _entero(_config(conexion, "default_dept_id", "1")) or 1
    sla_id = _entero(topic.get("sla_id")) or _entero(_config(conexion, "default_sla_id", "0"))
    status_id = _estado_por_defecto(conexion, _entero(topic.get("status_id")))
    prioridad = (
        _entero(prioridad_id)
        or _entero(topic.get("priority_id"))
        or _entero(_config(conexion, "default_priority_id", "2"))
        or 2
    )

    numero = _generar_numero(conexion, topic.get("number_format"))

    ticket_id = conexion.execute(
        text(f"""
            INSERT INTO {P}ticket
                (number, user_id, user_email_id, status_id, dept_id, sla_id, topic_id,
                 staff_id, team_id, email_id, lock_id, flags, sort, ip_address,
                 source, source_extra, isoverdue, isanswered,
                 lastupdate, created, updated)
            VALUES
                (:numero, :user_id, :user_email_id, :status_id, :dept_id, :sla_id, :topic_id,
                 :staff_id, :team_id, 0, 0, 0, 0, :ip,
                 'API', 'api-fallback', 0, 0,
                 NOW(), NOW(), NOW())
        """),
        {
            "numero": numero,
            "user_id": user_id,
            "user_email_id": user_email_id,
            "status_id": status_id,
            "dept_id": dept_id,
            "sla_id": sla_id,
            "topic_id": _entero(topic_id),
            "staff_id": _entero(topic.get("staff_id")),
            "team_id": _entero(topic.get("team_id")),
            "ip": (ip_origen or "")[:64],
        },
    ).lastrowid

    # El asunto y la prioridad viven en cdata (campos del formulario dinámico),
    # no en ost_ticket. Sin esta fila el ticket sale sin asunto en las colas.
    conexion.execute(
        text(f"""
            INSERT INTO {P}ticket__cdata (ticket_id, subject, priority)
            VALUES (:ticket_id, :asunto, :prioridad)
        """),
        {"ticket_id": ticket_id, "asunto": asunto, "prioridad": str(prioridad)},
    )

    thread_id = conexion.execute(
        text(f"""
            INSERT INTO {P}thread (object_id, object_type, lastmessage, created)
            VALUES (:ticket_id, 'T', NOW(), NOW())
        """),
        {"ticket_id": ticket_id},
    ).lastrowid

    # El mensaje puede venir como data URI ("data:text/html;charset=utf-8,...")
    # para indicar que es HTML. La API nativa lo interpreta sola; acá, que se
    # escribe directo en la base, hay que hacerlo a mano o el prefijo queda a
    # la vista del agente dentro del ticket. Y el formato tampoco se puede dar
    # por supuesto: marcar como HTML un texto plano hace que osTicket no
    # respete los saltos de línea.
    cuerpo = rfc2397.parsear_mensaje(mensaje)

    conexion.execute(
        text(f"""
            INSERT INTO {P}thread_entry
                (pid, thread_id, staff_id, user_id, type, flags, poster, source,
                 title, body, format, ip_address, created, updated)
            VALUES
                (0, :thread_id, 0, :user_id, 'M', 0, :poster, 'API',
                 :titulo, :cuerpo, :formato, :ip, NOW(), NOW())
        """),
        {
            "thread_id": thread_id,
            "user_id": user_id,
            "poster": (nombre or email)[:128],
            "titulo": asunto[:255],
            "cuerpo": cuerpo.texto,
            "formato": cuerpo.formato,
            "ip": (ip_origen or "")[:64],
        },
    )

    logger.warning(
        "Ticket %s creado por fallback SQL (ticket_id=%s): no se dispararon "
        "filtros, SLA ni notificaciones", numero, ticket_id
    )

    return {"numero": numero, "ticket_id": ticket_id, "user_id": user_id}


def responder_ticket(
    conexion: Connection,
    *,
    ticket_id: int,
    thread_id: int,
    user_id: int,
    poster: str,
    mensaje: str,
    ip_origen: str = "",
) -> Dict[str, Any]:
    """Agrega un mensaje del cliente al hilo de un ticket que ya existe.

    osTicket v1.18 no publica ninguna API nativa para esto (su dispatcher
    solo tiene POST /api/tickets.* para CREAR, ver upload/include/api.tickets.php):
    esto no es un fallback, es el único camino. Réplica mínima de lo que hace
    Ticket::onMessage() al llegar un mensaje del usuario (upload/include/
    class.ticket.php:1889): agrega la entrada del hilo y marca isanswered=0
    con el lastupdate/lastmessage al día.

    Lo que NO hace, a propósito:
      * no reabre el ticket si está cerrado (reopen() recalcula el SLA y
        reasigna el estado según configuración; replicarlo a mano es
        arriesgado). Si el ticket está cerrado, el mensaje igual se guarda,
        pero un agente tiene que reabrirlo a mano.
      * no manda alerta al agente asignado ni a los colaboradores.
      * no acepta adjuntos (mismo motivo que crear_ticket: escribir en
        ost_file/ost_file_chunk depende del backend de almacenamiento
        configurado).
    """
    cuerpo = rfc2397.parsear_mensaje(mensaje)

    entry_id = conexion.execute(
        text(f"""
            INSERT INTO {P}thread_entry
                (pid, thread_id, staff_id, user_id, type, flags, poster, source,
                 title, body, format, ip_address, created, updated)
            VALUES
                (0, :thread_id, 0, :user_id, 'M', 0, :poster, 'API',
                 '', :cuerpo, :formato, :ip, NOW(), NOW())
        """),
        {
            "thread_id": thread_id,
            "user_id": user_id,
            "poster": poster[:128],
            "cuerpo": cuerpo.texto,
            "formato": cuerpo.formato,
            "ip": (ip_origen or "")[:64],
        },
    ).lastrowid

    conexion.execute(
        text(f"UPDATE {P}thread SET lastmessage = NOW() WHERE id = :thread_id"),
        {"thread_id": thread_id},
    )
    conexion.execute(
        text(f"""
            UPDATE {P}ticket SET isanswered = 0, lastupdate = NOW()
            WHERE ticket_id = :ticket_id
        """),
        {"ticket_id": ticket_id},
    )

    logger.warning(
        "Mensaje %s agregado al ticket_id=%s por SQL directo: no se reabre "
        "si estaba cerrado ni se avisa al agente asignado", entry_id, ticket_id,
    )

    return {"mensaje_id": entry_id}
