"""Lecturas y escrituras de `api_cliente`, la tabla de clientes de esta API.

Va en un módulo aparte de ticket_repo porque esa tabla es nuestra y no de
osTicket: no lleva el prefijo configurable ni depende del esquema del
producto.
"""

import logging
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

TABLA = "api_cliente"


def listar_activos(conexion: Connection) -> List[Dict[str, Any]]:
    """Todos los clientes habilitados, para armar la caché de autenticación.

    Se traen todos de una vez en vez de consultar por clave en cada request:
    son pocas filas y la alternativa es una consulta por petición.
    """
    filas = conexion.execute(
        text(f"""
            SELECT nombre, clave_hash, org_id, ips_permitidas, permisos
            FROM {TABLA}
            WHERE activo = 1
        """)
    ).all()
    return [
        {
            "nombre": fila.nombre,
            "clave_hash": fila.clave_hash,
            "org_id": int(fila.org_id),
            "ips_permitidas": fila.ips_permitidas or "",
            "permisos": fila.permisos or "",
        }
        for fila in filas
    ]


def marcar_uso(conexion: Connection, nombre: str) -> None:
    """Deja constancia de que la clave sigue viva.

    Quien llama se encarga de no invocarlo en cada request (ver el acelerador
    de core.security): esto es un UPDATE, y una escritura por petición sería
    un costo alto para un dato que solo sirve para detectar claves muertas.
    """
    conexion.execute(
        text(f"UPDATE {TABLA} SET ultimo_uso = NOW() WHERE nombre = :nombre"),
        {"nombre": nombre},
    )


def tabla_existe(conexion: Connection) -> bool:
    """Distingue "no hay clientes dados de alta" de "falta correr el DDL".

    Sin esto, olvidar sql/001_api_cliente.sql se manifiesta como un 403 en
    todas las peticiones, que es un síntoma que no apunta a la causa.
    """
    return bool(
        conexion.execute(
            text("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema = DATABASE() AND table_name = :tabla
            """),
            {"tabla": TABLA},
        ).scalar()
    )
