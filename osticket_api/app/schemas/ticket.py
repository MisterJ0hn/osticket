from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.core.config import settings

# Los nombres de campo van en español de cara al consumidor; la traducción
# desde las columnas inglesas de osTicket se hace en el repositorio.


class FiltroEstado(str, Enum):
    """Filtro de alto nivel para el listado.

    Se apoya en `ost_ticket_status.state` y no en el id del estado, porque
    los estados son configurables en osTicket: un helpdesk puede agregar
    "En espera de cliente" con state='open' y el listado debe incluirlo.
    """

    ABIERTOS = "abiertos"
    CERRADOS = "cerrados"
    TODOS = "todos"


class TipoMensaje(str, Enum):
    MENSAJE = "mensaje"      # ost_thread_entry.type = 'M' (lo escribe el cliente)
    RESPUESTA = "respuesta"  # 'R' (lo escribe el agente)
    NOTA = "nota"            # 'N' (nota interna, no visible para el cliente)


class Adjunto(BaseModel):
    nombre: str = Field(..., max_length=255, description="Nombre del archivo")
    contenido_base64: str = Field(..., description="Contenido del archivo en base64")
    tipo_mime: str = Field("application/octet-stream", max_length=127)

    @property
    def tamano_bytes(self) -> int:
        """Tamaño del archivo ya decodificado, estimado desde el base64.

        No se decodifica para medirlo: eso duplicaría en memoria cada adjunto
        solo para validarlo. base64 son 4 caracteres por cada 3 bytes, menos
        el relleno final.
        """
        largo = len(self.contenido_base64.strip())
        relleno = self.contenido_base64.strip().count("=", -2)
        return max(0, (largo * 3) // 4 - relleno)


class CrearTicketRequest(BaseModel):
    email: EmailStr = Field(..., description="Email del usuario dueño del ticket")
    nombre: Optional[str] = Field(
        None, max_length=128,
        description="Nombre del usuario. Solo se usa si el email aún no existe en osTicket",
    )
    asunto: str = Field(..., min_length=1, max_length=255)
    mensaje: str = Field(..., min_length=1)
    telefono: Optional[str] = Field(None, max_length=24)
    topic_id: Optional[int] = Field(
        None, description="Id del tema de ayuda (ver GET /catalogos/temas)"
    )
    prioridad_id: Optional[int] = Field(
        None, description="Id de prioridad (ver GET /catalogos/prioridades)"
    )
    adjuntos: List[Adjunto] = Field(default_factory=list)
    # Ambos van a la API nativa; en el fallback SQL se ignoran porque ese
    # camino no manda correo de ninguna forma.
    alertar_agentes: bool = Field(
        True, description="Enviar la alerta de ticket nuevo a los agentes"
    )
    auto_responder: bool = Field(
        True, description="Enviar la auto-respuesta al usuario"
    )

    @model_validator(mode="after")
    def _validar_tamano_de_adjuntos(self) -> "CrearTicketRequest":
        """Rechaza de entrada los adjuntos que no van a caber.

        Sin esto, un envío demasiado grande no falla acá sino más adelante y
        de forma opaca: PHP corta el cuerpo o se queda sin memoria, osTicket
        contesta un 500 sin explicación, y la API lo lee como "el helpdesk
        está caído" y cae al fallback SQL... que descarta los adjuntos. El
        consumidor recibe un ticket creado a medias en vez de un error que
        le diga qué arreglar.
        """
        if not self.adjuntos:
            return self

        maximo = settings.ADJUNTOS_MAX_MB * 1024 * 1024
        total = sum(adjunto.tamano_bytes for adjunto in self.adjuntos)
        if total > maximo:
            raise ValueError(
                f"Los adjuntos suman {total / 1048576:.1f} MB y el máximo es "
                f"{settings.ADJUNTOS_MAX_MB} MB. Enviar menos archivos, o "
                f"comprimir las imágenes antes de adjuntarlas."
            )
        return self


class OrigenCreacion(str, Enum):
    NATIVA = "nativa"
    FALLBACK_SQL = "fallback_sql"


class CrearTicketResponse(BaseModel):
    exito: bool = True
    numero: str
    ticket_id: Optional[int] = None
    origen: OrigenCreacion
    mensaje: Optional[str] = None


class Usuario(BaseModel):
    id: Optional[int] = None
    nombre: Optional[str] = None
    email: Optional[str] = None


class TicketResumen(BaseModel):
    ticket_id: int
    numero: str
    asunto: Optional[str] = None
    estado: Optional[str] = None
    state: Optional[str] = None
    abierto: bool
    prioridad: Optional[str] = None
    departamento: Optional[str] = None
    tema: Optional[str] = None
    agente_asignado: Optional[str] = None
    usuario: Usuario
    creado: Optional[datetime] = None
    actualizado: Optional[datetime] = None
    cerrado: Optional[datetime] = None
    vencimiento: Optional[datetime] = None
    atrasado: bool = False
    respondido: bool = False


class MensajeHilo(BaseModel):
    id: int
    tipo: TipoMensaje
    autor: Optional[str] = None
    titulo: Optional[str] = None
    cuerpo: str
    formato: Optional[str] = None
    creado: Optional[datetime] = None
    adjuntos: List["AdjuntoInfo"] = Field(default_factory=list)


class AdjuntoInfo(BaseModel):
    id: int
    nombre: Optional[str] = None
    tipo_mime: Optional[str] = None
    tamano: Optional[int] = None
    inline: bool = Field(
        False,
        description="La imagen va incrustada en el cuerpo del mensaje, no "
                    "adjunta debajo. Pasa con los tickets creados desde el "
                    "portal web",
    )
    cid: Optional[str] = Field(
        None,
        description="Solo en los incrustados: el cuerpo los referencia como "
                    '<img src="cid:ESTE-VALOR">',
    )
    contenido_base64: Optional[str] = Field(
        None,
        description="Contenido del archivo. Solo viene si se pidió con "
                    "incluir_contenido=true",
    )
    error: Optional[str] = Field(
        None, description="Por qué no se pudo devolver el contenido"
    )


class TicketDetalle(TicketResumen):
    fuente: Optional[str] = None
    ip_origen: Optional[str] = None
    ultima_respuesta: Optional[datetime] = None
    ultimo_mensaje: Optional[datetime] = None
    mensajes: List[MensajeHilo] = Field(default_factory=list)


class ListaTicketsResponse(BaseModel):
    exito: bool = True
    total: int
    pagina: int
    tamano: int
    tickets: List[TicketResumen]


class EstadoTicketResponse(BaseModel):
    exito: bool = True
    numero: str
    estado: Optional[str] = None
    state: Optional[str] = None
    abierto: bool
    agente_asignado: Optional[str] = None
    creado: Optional[datetime] = None
    actualizado: Optional[datetime] = None
    cerrado: Optional[datetime] = None
    vencimiento: Optional[datetime] = None
    atrasado: bool = False


class ItemCatalogo(BaseModel):
    id: int
    nombre: str
    # Solo lo llenan los estados: sirve para saber cuáles cuentan como abiertos.
    state: Optional[str] = None


class RespuestaError(BaseModel):
    exito: bool = False
    mensaje: str


class FiltroFechas(BaseModel):
    desde: Optional[date] = None
    hasta: Optional[date] = None


MensajeHilo.model_rebuild()
