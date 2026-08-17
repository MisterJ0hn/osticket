import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from app.core.config import settings

logger = logging.getLogger(__name__)

# No se mapean modelos ORM sobre el esquema de osTicket: es un esquema ajeno,
# que cambia con cada upgrade del producto y que acá solo se consulta. Se usa
# el engine de SQLAlchemy por el pool y se escriben las consultas con text().
engine = create_engine(
    settings.mysql_url,
    pool_size=settings.MYSQL_POOL_SIZE,
    max_overflow=settings.MYSQL_MAX_OVERFLOW,
    pool_recycle=settings.MYSQL_POOL_RECYCLE,
    pool_pre_ping=True,
    future=True,
)


def obtener_conexion() -> Iterator[Connection]:
    """Dependencia FastAPI: una conexión de solo lectura por request."""
    with engine.connect() as conexion:
        yield conexion


@contextmanager
def transaccion() -> Iterator[Connection]:
    """Conexión con transacción para el fallback de escritura.

    engine.begin() hace commit al salir sin excepción y rollback si la hay,
    que es justo lo que necesita la creación de un ticket: o quedan todas
    sus filas (ticket, cdata, thread, entrada) o no queda ninguna.
    """
    with engine.begin() as conexion:
        yield conexion


def verificar_conexion() -> bool:
    """Ping usado por /salud y por el arranque, para fallar con un mensaje
    claro en vez de con un stacktrace en el primer request real."""
    try:
        with engine.connect() as conexion:
            conexion.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("No se pudo conectar a la base de osTicket")
        return False


def verificar_esquema() -> Optional[str]:
    """Comprueba que la base configurada sea de verdad la de osTicket.

    Devuelve None si todo está bien, o un mensaje describiendo el problema.

    Conectar no basta. En un mismo servidor suele haber varias bases (aquí
    conviven 'osticket', 'temposoft_soporte' y 'u551401919_soporte'), y
    apuntar a la equivocada no da error de conexión: daría listados vacíos o,
    peor, tickets de otro helpdesk. Por eso se verifica que exista la tabla
    de tickets con el prefijo configurado.
    """
    tabla = f"{settings.prefijo}ticket"
    try:
        with engine.connect() as conexion:
            existe = conexion.execute(
                text("""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = DATABASE() AND table_name = :tabla
                """),
                {"tabla": tabla},
            ).scalar()
    except Exception as exc:
        return f"no se pudo consultar el esquema: {exc}"

    if not existe:
        return (
            f"la base '{settings.MYSQL_DB}' no tiene la tabla '{tabla}': "
            f"revisa MYSQL_DB y OSTICKET_TABLE_PREFIX, porque no parece ser "
            f"la base de osTicket"
        )
    return None
