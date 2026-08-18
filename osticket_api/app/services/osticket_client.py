"""Cliente de la API nativa de osTicket.

osTicket v1.18 publica un único endpoint de escritura:
`POST /api/tickets.json` autenticado con el header `X-API-Key`
(ver upload/api/http.php y upload/include/api.tickets.php). Responde 201 con
el número del ticket en texto plano; cualquier otro código trae el motivo en
el cuerpo.
"""

import base64
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.exceptions import OsticketApiError

logger = logging.getLogger(__name__)

# Campos que acepta el parser JSON de osTicket. Enviar cualquier otro hace que
# la petición se rechace entera con "Unexpected or invalid data received", así
# que el payload se arma explícitamente y no a partir del request del cliente.
_CAMPOS_SOPORTADOS = {
    "name", "email", "phone", "subject", "message", "ip",
    "topicId", "priorityId", "alert", "autorespond", "source", "attachments",
}


def _adjuntos_a_formato_osticket(adjuntos: List[Any]) -> List[Dict[str, str]]:
    """Convierte los adjuntos al formato que espera el parser JSON.

    ApiJsonDataParser::fixup() (upload/include/class.api.php:466) espera una
    lista de diccionarios de UN elemento, donde la clave es el nombre del
    archivo y el valor es una URI RFC 2397:
        [{"informe.pdf": "data:application/pdf;base64,JVBERi0..."}]
    La forma estructurada {name, type, data, encoding} que documenta
    getRequestStructure() solo aplica al formato email/XML; en JSON rompe.
    """
    convertidos = []
    for adjunto in adjuntos:
        contenido = adjunto.contenido_base64.strip()
        # Se valida acá y no en osTicket para poder devolver un 400 con un
        # mensaje entendible en vez del error genérico del helpdesk.
        try:
            base64.b64decode(contenido, validate=True)
        except Exception as exc:
            raise OsticketApiError(
                f"El adjunto '{adjunto.nombre}' no es base64 válido", status_code=400
            ) from exc
        convertidos.append({
            adjunto.nombre: f"data:{adjunto.tipo_mime};base64,{contenido}"
        })
    return convertidos


def construir_payload(
    email: str,
    asunto: str,
    mensaje: str,
    nombre: Optional[str] = None,
    telefono: Optional[str] = None,
    topic_id: Optional[int] = None,
    prioridad_id: Optional[int] = None,
    adjuntos: Optional[List[Any]] = None,
    ip_origen: str = "",
    alertar_agentes: bool = True,
    auto_responder: bool = True,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        # osTicket exige un nombre; si el usuario ya existe lo ignora y usa
        # el que tiene registrado.
        "name": nombre or email.split("@")[0],
        "email": email,
        "subject": asunto,
        "message": mensaje,
        "source": "API",
        "alert": alertar_agentes,
        "autorespond": auto_responder,
    }
    if ip_origen:
        payload["ip"] = ip_origen
    if telefono:
        payload["phone"] = telefono
    if topic_id:
        payload["topicId"] = topic_id
    if prioridad_id:
        payload["priorityId"] = prioridad_id
    if adjuntos:
        payload["attachments"] = _adjuntos_a_formato_osticket(adjuntos)

    assert set(payload) <= _CAMPOS_SOPORTADOS, "Campo no soportado por la API de osTicket"
    return payload


def crear_ticket(payload: Dict[str, Any]) -> str:
    """Crea el ticket en osTicket y devuelve su número.

    Levanta OsticketApiError con `es_transporte=True` cuando osTicket no
    contestó (conexión, timeout, 5xx) o rechazó la clave: son los casos en
    que tiene sentido el fallback a SQL.
    """
    if not settings.OSTICKET_API_KEY:
        raise OsticketApiError(
            "OSTICKET_API_KEY no está configurada", status_code=None, es_transporte=True
        )

    url = f"{settings.osticket_url_base}/api/tickets.json"
    cabeceras = {
        "X-API-Key": settings.OSTICKET_API_KEY,
        "Content-Type": "application/json",
        # osTicket registra el user agent; identificarse ayuda a rastrear
        # de dónde salió un ticket desde el panel.
        "User-Agent": "temposoft-osticket-api/1.0",
        # Sin esto, httpx puede mandar "Expect: 100-continue" con cuerpos
        # grandes (adjuntos) y Apache responde 417.
        "Expect": "",
    }

    # Con adjuntos se usa un timeout mucho más largo. No es solo para no
    # fallar de más: si rendimos antes que osTicket, él sigue procesando y
    # puede acabar creando el ticket mientras nosotros ya lo dimos por
    # perdido y lo creamos otra vez por SQL. El resultado sería dos tickets
    # para la misma solicitud, y el duplicado no lo detecta nadie.
    timeout = (settings.OSTICKET_TIMEOUT_ADJUNTOS if payload.get("attachments")
               else settings.OSTICKET_TIMEOUT)

    try:
        respuesta = httpx.post(url, json=payload, headers=cabeceras, timeout=timeout)
    except httpx.HTTPError as exc:
        raise OsticketApiError(
            f"No se pudo contactar la API de osTicket en {url}: {exc}",
            es_transporte=True,
        ) from exc

    cuerpo = (respuesta.text or "").strip()

    if respuesta.status_code == 201:
        if not cuerpo:
            raise OsticketApiError(
                "osTicket respondió 201 sin número de ticket", status_code=201
            )
        return cuerpo

    # osTicket devuelve 500 TAMBIÉN para errores de validación: cuando
    # Ticket::create() falla, api.tickets.php:172 responde
    # exerr($errors['errno'] ?: 500, "Unable to create new ticket :<motivo>").
    # Sin distinguirlo, un email inválido o un tema inexistente acabarían
    # creando el ticket por SQL, que es justo saltarse la validación.
    # Ese prefijo solo lo emite osTicket habiendo procesado la petición; un
    # 500 de Apache o un fatal de PHP no lo llevan.
    rechazo_de_osticket = "unable to create new ticket" in cuerpo.lower()

    if rechazo_de_osticket:
        es_transporte = False
    else:
        # 401/403: la clave no está dada de alta o la IP no está autorizada.
        # Es un problema de configuración, no de los datos: conviene que el
        # fallback salve el ticket mientras se corrige.
        # 5xx restantes: el helpdesk está caído de verdad.
        es_transporte = respuesta.status_code in (401, 403) or respuesta.status_code >= 500

    raise OsticketApiError(
        f"osTicket rechazó la creación (HTTP {respuesta.status_code}): {cuerpo}",
        status_code=respuesta.status_code,
        cuerpo=cuerpo,
        es_transporte=es_transporte,
    )
