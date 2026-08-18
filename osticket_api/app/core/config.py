import re
from functools import cached_property
from typing import List
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# El prefijo de tabla se interpola literalmente en el SQL (no se puede ligar
# como parámetro un nombre de tabla), así que se restringe a lo que osTicket
# admite como TABLE_PREFIX. Es configuración nuestra, no dato de request,
# pero validarlo evita que un .env mal escrito abra una inyección.
_PREFIJO_VALIDO = re.compile(r"^[A-Za-z0-9_]*$")


def _lista(valor: str) -> List[str]:
    """Convierte 'a, b ,c' en ['a', 'b', 'c'], descartando vacíos."""
    return [item.strip() for item in valor.split(",") if item.strip()]


class Settings(BaseSettings):
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    # ── MySQL de osTicket ──
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    # Se acepta también MYSQL_DATABASE: es el nombre que usan las imágenes
    # oficiales de MySQL/MariaDB, así que es el que sale natural al copiar la
    # configuración desde un docker-compose. Sin el alias, esa variable se
    # ignoraría en silencio y la base caería al valor por defecto.
    MYSQL_DB: str = Field(
        "osticket",
        validation_alias=AliasChoices("MYSQL_DB", "MYSQL_DATABASE"),
    )
    MYSQL_USER: str = "osticket"
    MYSQL_PASSWORD: str = ""
    MYSQL_POOL_SIZE: int = 5
    MYSQL_MAX_OVERFLOW: int = 5
    # Hostinger corta las conexiones ociosas; reciclar antes evita el
    # "MySQL server has gone away" en el primer request tras un rato quieto.
    MYSQL_POOL_RECYCLE: int = 1800

    OSTICKET_TABLE_PREFIX: str = "ost_"

    # ── API nativa de osTicket ──
    OSTICKET_URL: str = "http://localhost:8080"
    OSTICKET_API_KEY: str = ""
    OSTICKET_TIMEOUT: float = 15.0
    # Timeout aparte para las peticiones CON adjuntos: subir varios MB y que
    # osTicket los decodifique y los escriba en la base no entra en 15s.
    #
    # Tiene que ser >= al max_execution_time de PHP (120s en docker/php.ini).
    # Si rendimos antes que osTicket, él sigue procesando y puede terminar
    # creando el ticket mientras nosotros ya lo dimos por perdido y lo
    # creamos de nuevo por SQL: dos tickets para la misma solicitud.
    OSTICKET_TIMEOUT_ADJUNTOS: float = 120.0
    # Tamaño máximo del conjunto de adjuntos de un ticket, ya decodificados.
    # Por debajo del max_file_size de osTicket (32 MB) para poder rechazarlo
    # con un mensaje claro en vez de que reviente más adelante.
    ADJUNTOS_MAX_MB: int = 25
    # Tope de lo que se devuelve en un GET de detalle con
    # incluir_contenido=true. Los adjuntos van en base64 dentro del JSON,
    # así que ocupan ~33% más que el archivo y se arman enteros en memoria:
    # sin tope, un ticket con varias fotos tumba el proceso.
    ADJUNTOS_DESCARGA_MAX_MB: int = 15
    OSTICKET_FALLBACK_SQL: bool = True

    # ── Seguridad de este servicio ──
    # Claves anónimas heredadas: ven TODO el helpdesk, sin filtro de
    # organización. Se mantienen solo para migrar; lo nuevo va en la tabla
    # api_cliente. Ver app/core/security.py.
    API_KEYS: str = ""
    # Cada cuánto se relee api_cliente. Marca el retardo con el que una baja
    # (activo = 0) surte efecto: bajarlo cuesta una consulta más seguido.
    CACHE_CLIENTES_TTL: int = 60
    IPS_PERMITIDAS: str = ""
    TRUSTED_PROXIES: str = ""
    CORS_ORIGINS: str = "*"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @cached_property
    def mysql_url(self) -> str:
        # quote_plus en la contraseña: la de este osTicket lleva '&' y sin
        # escapar rompe el parseo de la URL de SQLAlchemy.
        return (
            f"mysql+pymysql://{quote_plus(self.MYSQL_USER)}:"
            f"{quote_plus(self.MYSQL_PASSWORD)}@{self.MYSQL_HOST}:{self.MYSQL_PORT}/"
            f"{self.MYSQL_DB}?charset=utf8mb4"
        )

    @cached_property
    def prefijo(self) -> str:
        prefijo = self.OSTICKET_TABLE_PREFIX
        if not _PREFIJO_VALIDO.match(prefijo):
            raise ValueError(
                f"OSTICKET_TABLE_PREFIX inválido: {prefijo!r}. "
                "Solo se admiten letras, dígitos y guion bajo."
            )
        return prefijo

    @cached_property
    def api_keys(self) -> List[str]:
        return _lista(self.API_KEYS)

    @cached_property
    def ips_permitidas(self) -> List[str]:
        return _lista(self.IPS_PERMITIDAS)

    @cached_property
    def trusted_proxies(self) -> List[str]:
        return _lista(self.TRUSTED_PROXIES)

    @cached_property
    def cors_origins(self) -> List[str]:
        return _lista(self.CORS_ORIGINS) or ["*"]

    @cached_property
    def osticket_url_base(self) -> str:
        return self.OSTICKET_URL.rstrip("/")


settings = Settings()
