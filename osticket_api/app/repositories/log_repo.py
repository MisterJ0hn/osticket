"""Lecturas y escrituras de `api_llamada`, el log de llamados a esta API.

Va en un módulo aparte de ticket_repo por lo mismo que cliente_repo: es una
tabla nuestra, no de osTicket, sin el prefijo configurable.
"""

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

TABLA = "api_llamada"


def registrar(
    conexion: Connection,
    *,
    ip: str,
    metodo: str,
    request: Optional[str],
    response_code: int,
    response: Optional[str],
) -> None:
    """Inserta una fila del log. Quien llama decide qué hacer si falla."""
    conexion.execute(
        text(f"""
            INSERT INTO {TABLA}
                (fecha, ip, metodo, request, response_code, response)
            VALUES
                (NOW(), :ip, :metodo, :request, :response_code, :response)
        """),
        {
            "ip": ip,
            "metodo": metodo,
            "request": request,
            "response_code": response_code,
            "response": response,
        },
    )


def tabla_existe(conexion: Connection) -> bool:
    """Distingue "no se corrió sql/002_api_llamada.sql" de un fallo real."""
    return bool(
        conexion.execute(
            text("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema = DATABASE() AND table_name = :tabla
            """),
            {"tabla": TABLA},
        ).scalar()
    )
