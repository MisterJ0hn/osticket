class ErrorDominio(Exception):
    """Error esperable, con una traducción directa a código HTTP.

    Los routers no atrapan estas excepciones una por una: hay un handler en
    main.py que las convierte en la respuesta uniforme
    {"exito": false, "mensaje": "..."}.
    """

    http_status = 400

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


class RecursoNoEncontrado(ErrorDominio):
    http_status = 404


class DatosInvalidos(ErrorDominio):
    http_status = 400


class ErrorCreacionTicket(ErrorDominio):
    """No se pudo crear el ticket ni por la API nativa ni por el fallback."""

    http_status = 502


class OsticketApiError(Exception):
    """Fallo hablando con la API nativa de osTicket.

    `es_transporte` distingue los dos casos que el servicio trata distinto:
    si osTicket no contestó (conexión caída, timeout, 5xx) tiene sentido el
    fallback a SQL; si contestó 4xx, el problema son los datos y repetir por
    SQL solo saltaría la validación.
    """

    def __init__(self, mensaje: str, status_code: int | None = None,
                 cuerpo: str = "", es_transporte: bool = False):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.status_code = status_code
        self.cuerpo = cuerpo
        self.es_transporte = es_transporte
