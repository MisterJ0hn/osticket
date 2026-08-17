"""Orquestación: qué camino usa cada operación y cómo se combinan."""

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
from app.repositories import ticket_repo, ticket_write_repo
from app.schemas.ticket import CrearTicketRequest, FiltroEstado, OrigenCreacion
from app.services import osticket_client

logger = logging.getLogger(__name__)


def crear_ticket(datos: CrearTicketRequest, ip_origen: str = "") -> Dict[str, Any]:
    """Crea el ticket por la API nativa y, si no se puede, por SQL.

    La API nativa es el camino bueno: aplica filtros, SLA, auto-asignación,
    auto-respuesta y alertas. El fallback existe para no perder el ticket
    cuando el helpdesk no responde, pero se salta todo eso.
    """
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
        return _crear_por_sql(datos, ip_origen, motivo=error.mensaje)

    return {
        "numero": numero,
        # La API nativa devuelve el número, no el id interno.
        "ticket_id": _resolver_ticket_id(numero),
        "origen": OrigenCreacion.NATIVA,
        "mensaje": None,
    }


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


def _crear_por_sql(datos: CrearTicketRequest, ip_origen: str, motivo: str) -> Dict[str, Any]:
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
            )
    except Exception as error:
        logger.exception("Falló también el fallback SQL")
        raise ErrorCreacionTicket(
            f"No se pudo crear el ticket. API nativa: {motivo}. Fallback SQL: {error}"
        ) from error

    aviso = ("Creado por el fallback SQL: no se enviaron notificaciones ni se "
             "aplicaron filtros/SLA.")
    if datos.adjuntos:
        aviso += " Los adjuntos no se guardaron."

    return {
        "numero": resultado["numero"],
        "ticket_id": resultado["ticket_id"],
        "origen": OrigenCreacion.FALLBACK_SQL,
        "mensaje": aviso,
    }


def listar_tickets(
    conexion: Connection,
    email: str,
    estado: FiltroEstado = FiltroEstado.ABIERTOS,
    estado_nombre: Optional[str] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    pagina: int = 1,
    tamano: int = 25,
) -> Dict[str, Any]:
    usuario = ticket_repo.buscar_usuario_por_email(conexion, email)
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
    )
    return {"total": total, "pagina": pagina, "tamano": tamano, "tickets": tickets}


def obtener_detalle(conexion: Connection, numero: str,
                    incluir_notas: bool = False) -> Dict[str, Any]:
    detalle = ticket_repo.obtener_ticket(conexion, numero)
    if not detalle:
        raise RecursoNoEncontrado(f"No existe el ticket {numero}")

    thread_id = detalle.pop("_thread_id", None)
    detalle["mensajes"] = ticket_repo.obtener_hilo(conexion, thread_id, incluir_notas)
    return detalle


def obtener_estado(conexion: Connection, numero: str) -> Dict[str, Any]:
    estado = ticket_repo.obtener_estado(conexion, numero)
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
