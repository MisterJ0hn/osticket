"""Orquestación: qué camino usa cada operación y cómo se combinan."""

import base64
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy.engine import Connection

from app.core import database
from app.core.config import settings
from app.core.exceptions import (
    DatosInvalidos,
    ErrorCreacionTicket,
    OsticketApiError,
    RecursoNoEncontrado,
)
from app.core.security import ORG_TODAS
from app.repositories import ticket_repo, ticket_write_repo
from app.schemas.ticket import (
    CrearTicketRequest,
    FiltroEstado,
    OrigenCreacion,
    ResponderTicketRequest,
)
from app.services import osticket_client

logger = logging.getLogger(__name__)


def crear_ticket(datos: CrearTicketRequest, ip_origen: str = "",
                 org_id: int = ORG_TODAS) -> Dict[str, Any]:
    """Crea el ticket por la API nativa y, si no se puede, por SQL.

    La API nativa es el camino bueno: aplica filtros, SLA, auto-asignación,
    auto-respuesta y alertas. El fallback existe para no perder el ticket
    cuando el helpdesk no responde, pero se salta todo eso.

    `org_id` es la organización del cliente que llama. El usuario dueño del
    ticket tiene que quedar en ella, o el propio cliente no podrá leer
    después el ticket que acaba de crear.
    """
    _validar_organizacion(datos.email, org_id)

    try:
        # Armar el payload también puede fallar (un adjunto que no es base64
        # válido), y eso es un error de datos: va dentro del mismo try para
        # que salga como 400 y no como 500.
        payload = osticket_client.construir_payload(
            email=str(datos.email),
            asunto=datos.asunto,
            mensaje=datos.mensaje,
            nombre=datos.nombre,
            telefono=datos.telefono,
            topic_id=datos.topic_id,
            prioridad_id=datos.prioridad_id,
            adjuntos=datos.adjuntos,
            ip_origen=ip_origen,
            alertar_agentes=datos.alertar_agentes,
            auto_responder=datos.auto_responder,
        )
        numero = osticket_client.crear_ticket(payload)
    except OsticketApiError as error:
        if not error.es_transporte:
            # osTicket contestó rechazando los datos (campos obligatorios del
            # formulario, tema inexistente, email en lista negra...). Repetir
            # por SQL solo crearía un ticket saltándose esa validación.
            raise DatosInvalidos(error.mensaje) from error

        if not settings.OSTICKET_FALLBACK_SQL:
            raise ErrorCreacionTicket(
                f"{error.mensaje}. El fallback SQL está desactivado."
            ) from error

        logger.warning("API nativa no disponible (%s); se usa el fallback SQL",
                       error.mensaje)
        return _crear_por_sql(datos, ip_origen, motivo=error.mensaje, org_id=org_id)

    # osTicket crea el usuario por su cuenta cuando el email es nuevo, y lo
    # deja sin organización. Hay que ponérsela acá o el ticket recién creado
    # queda invisible para el cliente que lo pidió.
    _asegurar_organizacion(str(datos.email), org_id)

    return {
        "numero": numero,
        # La API nativa devuelve el número, no el id interno.
        "ticket_id": _resolver_ticket_id(numero),
        "origen": OrigenCreacion.NATIVA,
        "mensaje": None,
    }


def _validar_organizacion(email: str, org_id: int) -> None:
    """Rechaza crear un ticket para un email que es de otra organización.

    Dejarlo pasar daría el peor resultado posible: un ticket que el cliente
    que lo pidió no puede leer, y que sí ve otro cliente distinto.

    Si no se puede consultar la base no se bloquea la creación: el fallback
    SQL existe justamente para cuando la infraestructura falla, y perder el
    ticket es peor que crearlo con la organización mal puesta (que además se
    corrige con un UPDATE).
    """
    if org_id == ORG_TODAS:
        return

    try:
        with database.engine.connect() as conexion:
            org_actual = ticket_write_repo.organizacion_del_email(conexion, email)
    except Exception:
        logger.exception("No se pudo verificar la organización de %s", email)
        return

    if org_actual is not None and org_actual not in (0, org_id):
        raise DatosInvalidos(
            f"El email {email} pertenece a otra organización de osTicket"
        )


def _asegurar_organizacion(email: str, org_id: int) -> None:
    """Asigna la organización al usuario si quedó sin ella.

    Es un paso posterior a la creación: si falla, el ticket ya existe y no
    tiene sentido devolver error. Queda en el log, y se arregla con el
    UPDATE que hace esta misma función.
    """
    if org_id == ORG_TODAS:
        return

    try:
        with database.transaccion() as conexion:
            ticket_write_repo.asignar_organizacion(conexion, email, org_id)
    except Exception:
        logger.exception(
            "El ticket se creó, pero no se pudo asignar %s a la organización "
            "%s: el cliente no lo verá en sus listados", email, org_id,
        )


def _resolver_ticket_id(numero: str) -> Optional[int]:
    """Busca el id interno del ticket recién creado.

    Es un dato informativo: si la consulta falla, el ticket igual existe y no
    tiene sentido devolver un error por eso.
    """
    try:
        with database.engine.connect() as conexion:
            cabecera = ticket_repo.obtener_ticket(conexion, numero)
            return cabecera["ticket_id"] if cabecera else None
    except Exception:
        logger.exception("No se pudo resolver el ticket_id de %s", numero)
        return None


def _crear_por_sql(datos: CrearTicketRequest, ip_origen: str, motivo: str,
                   org_id: int = ORG_TODAS) -> Dict[str, Any]:
    if datos.adjuntos:
        # Guardar adjuntos exige escribir en ost_file/ost_file_chunk con el
        # esquema de almacenamiento que tenga configurado el helpdesk. Se
        # crea el ticket igual (mejor eso que perderlo) y se avisa.
        logger.warning("El fallback SQL no guarda adjuntos: se descartan %d",
                       len(datos.adjuntos))

    try:
        with database.transaccion() as conexion:
            resultado = ticket_write_repo.crear_ticket(
                conexion,
                email=str(datos.email),
                asunto=datos.asunto,
                mensaje=datos.mensaje,
                nombre=datos.nombre,
                telefono=datos.telefono,
                topic_id=datos.topic_id,
                prioridad_id=datos.prioridad_id,
                ip_origen=ip_origen,
                org_id=org_id,
            )
    except Exception as error:
        logger.exception("Falló también el fallback SQL")
        raise ErrorCreacionTicket(
            f"No se pudo crear el ticket. API nativa: {motivo}. Fallback SQL: {error}"
        ) from error

    # El motivo va DENTRO del mensaje que ve el consumidor. Sin él, quien
    # recibe la respuesta (o el usuario del CRM al que se la muestran) solo
    # sabe que algo salió mal y tiene que pedirle los logs a otra persona
    # para averiguar qué. Con la causa a la vista, "la clave de la API no
    # está autorizada" se distingue de "el helpdesk está caído".
    aviso = ("Creado por el fallback SQL: no se enviaron notificaciones ni se "
             f"aplicaron filtros/SLA. Motivo: {motivo}")
    if datos.adjuntos:
        aviso += (f" ATENCIÓN: los {len(datos.adjuntos)} adjuntos NO se "
                  "guardaron; hay que volver a subirlos al ticket a mano.")

    return {
        "numero": resultado["numero"],
        "ticket_id": resultado["ticket_id"],
        "origen": OrigenCreacion.FALLBACK_SQL,
        "mensaje": aviso,
    }


def responder_ticket(datos: ResponderTicketRequest, numero: str,
                     ip_origen: str = "", org_id: int = ORG_TODAS) -> Dict[str, Any]:
    """Agrega un mensaje del cliente a un ticket que ya existe.

    No hay API nativa para esto en osTicket v1.18 (solo publica creación de
    tickets), así que se escribe directo en la base: ver las limitaciones
    documentadas en ticket_write_repo.responder_ticket (no reabre un ticket
    cerrado, no avisa al agente, no acepta adjuntos).

    Se exige que `email` coincida con el dueño del ticket por lo mismo que
    _validar_organizacion en la creación: dejar que cualquier email de la
    organización responda cualquier ticket permitiría que un cliente vea
    (a través del contenido del hilo, si el consumidor se lo devuelve) datos
    de un ticket que no es suyo.
    """
    with database.transaccion() as conexion:
        ticket = ticket_repo.obtener_ticket(conexion, numero, org_id=org_id)
        if not ticket:
            raise RecursoNoEncontrado(f"No existe el ticket {numero}")

        propietario = (ticket["usuario"]["email"] or "").strip().lower()
        if propietario != str(datos.email).strip().lower():
            raise DatosInvalidos("El email no coincide con el dueño del ticket")

        resultado = ticket_write_repo.responder_ticket(
            conexion,
            ticket_id=ticket["ticket_id"],
            thread_id=ticket["_thread_id"],
            user_id=ticket["usuario"]["id"],
            poster=ticket["usuario"]["nombre"] or str(datos.email),
            mensaje=datos.mensaje,
            ip_origen=ip_origen,
        )

    return {"numero": numero, "mensaje_id": resultado["mensaje_id"]}


def listar_tickets(
    conexion: Connection,
    email: str,
    estado: FiltroEstado = FiltroEstado.ABIERTOS,
    estado_nombre: Optional[str] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    pagina: int = 1,
    tamano: int = 25,
    org_id: int = ORG_TODAS,
) -> Dict[str, Any]:
    # El mensaje es el mismo para "no existe" y para "es de otra
    # organización": son el mismo 404 a propósito, para no confirmarle a un
    # cliente que cierta persona está registrada en el helpdesk de otro.
    usuario = ticket_repo.buscar_usuario_por_email(conexion, email, org_id=org_id)
    if not usuario:
        raise RecursoNoEncontrado(f"No existe un usuario con el email {email} en osTicket")

    total, tickets = ticket_repo.listar_tickets_de_usuario(
        conexion,
        user_id=usuario["id"],
        estado=estado,
        estado_nombre=estado_nombre,
        desde=desde,
        hasta=hasta,
        pagina=pagina,
        tamano=tamano,
        org_id=org_id,
    )
    return {"total": total, "pagina": pagina, "tamano": tamano, "tickets": tickets}


def obtener_detalle(conexion: Connection, numero: str,
                    incluir_notas: bool = False,
                    org_id: int = ORG_TODAS,
                    incluir_contenido: bool = False) -> Dict[str, Any]:
    detalle = ticket_repo.obtener_ticket(conexion, numero, org_id=org_id)
    if not detalle:
        raise RecursoNoEncontrado(f"No existe el ticket {numero}")

    thread_id = detalle.pop("_thread_id", None)
    mensajes = ticket_repo.obtener_hilo(conexion, thread_id, incluir_notas)

    if incluir_contenido:
        _adjuntar_contenido(conexion, mensajes)
    else:
        # El campo interno del backend no se publica nunca.
        for mensaje in mensajes:
            for adjunto in mensaje["adjuntos"]:
                adjunto.pop("_bk", None)
                adjunto.pop("file_id", None)

    detalle["mensajes"] = mensajes
    return detalle


def _adjuntar_contenido(conexion: Connection, mensajes: List[Dict[str, Any]]) -> None:
    """Rellena `contenido_base64` de cada adjunto del hilo.

    Se resuelve en UNA consulta para todo el ticket, no una por archivo: un
    hilo largo con capturas en varias respuestas haría decenas de viajes a
    la base para armar una sola respuesta.

    Los que no se pueden entregar se devuelven igual, con `error` explicando
    por qué. Omitirlos en silencio dejaría al consumidor creyendo que el
    mensaje no tenía imagen.
    """
    adjuntos = [a for mensaje in mensajes for a in mensaje["adjuntos"]]
    if not adjuntos:
        return

    maximo = settings.ADJUNTOS_DESCARGA_MAX_MB * 1024 * 1024
    descargables = []
    acumulado = 0

    for adjunto in adjuntos:
        backend = adjunto.pop("_bk", None)
        file_id = adjunto.pop("file_id", None)

        if backend and backend != "D":
            # osTicket admite guardar los archivos en disco o en un servicio
            # externo (ost_file.bk). Solo el backend 'D' guarda el contenido
            # en la base, que es lo único alcanzable desde acá.
            adjunto["error"] = (
                f"El archivo está en el almacenamiento '{backend}' de osTicket, "
                "no en la base: no se puede leer desde esta API"
            )
            continue

        tamano = adjunto.get("tamano") or 0
        if acumulado + tamano > maximo:
            adjunto["error"] = (
                f"Se superó el máximo de {settings.ADJUNTOS_DESCARGA_MAX_MB} MB "
                "de contenido por respuesta. Pedir el ticket sin "
                "incluir_contenido y descargar este adjunto aparte"
            )
            continue

        acumulado += tamano
        if file_id:
            descargables.append((file_id, adjunto))

    if not descargables:
        return

    contenidos = ticket_repo.contenido_de_archivos(
        conexion, [file_id for file_id, _ in descargables]
    )

    for file_id, adjunto in descargables:
        datos = contenidos.get(file_id)
        if datos is None:
            adjunto["error"] = "El archivo no tiene contenido en la base"
        else:
            adjunto["contenido_base64"] = base64.b64encode(datos).decode("ascii")


def obtener_estado(conexion: Connection, numero: str,
                   org_id: int = ORG_TODAS) -> Dict[str, Any]:
    estado = ticket_repo.obtener_estado(conexion, numero, org_id=org_id)
    if not estado:
        raise RecursoNoEncontrado(f"No existe el ticket {numero}")
    return estado


def catalogo(conexion: Connection, nombre: str) -> List[Dict[str, Any]]:
    lectores = {
        "temas": ticket_repo.listar_temas,
        "estados": ticket_repo.listar_estados,
        "prioridades": ticket_repo.listar_prioridades,
    }
    return lectores[nombre](conexion)
