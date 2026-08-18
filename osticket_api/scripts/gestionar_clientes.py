"""Alta, baja, rotación y listado de los clientes de la API.

    python -m scripts.gestionar_clientes alta --nombre intranet --org-id 2
    python -m scripts.gestionar_clientes listar
    python -m scripts.gestionar_clientes rotar --nombre intranet
    python -m scripts.gestionar_clientes baja  --nombre intranet

Se ejecuta donde esté configurada la conexión a MySQL (el mismo .env que usa
el servicio). Dentro del contenedor:

    docker compose exec api python -m scripts.gestionar_clientes listar

La clave se genera acá y se muestra UNA sola vez: en la base solo queda su
sha256. Si se pierde, no se recupera — se rota, que es justamente lo que hace
que perderla no sea un problema.
"""

import argparse
import secrets
import sys
from typing import Optional

from sqlalchemy import text

from app.core import database
from app.core.security import ORG_TODAS, PERMISO_CREAR, PERMISO_LEER, hash_clave
from app.repositories.cliente_repo import TABLA, tabla_existe

PERMISOS_POR_DEFECTO = f"{PERMISO_CREAR},{PERMISO_LEER}"


def _generar_clave() -> str:
    """32 bytes de entropía criptográfica en base64 url-safe.

    No se pide una clave por parámetro a propósito: una elegida a mano acaba
    siendo débil o repetida entre clientes, y además quedaría en el historial
    del shell.
    """
    return secrets.token_urlsafe(32)


def _existe(conexion, nombre: str) -> bool:
    return bool(
        conexion.execute(
            text(f"SELECT 1 FROM {TABLA} WHERE nombre = :nombre"), {"nombre": nombre}
        ).first()
    )


def _confirmar_org_todas() -> bool:
    """org_id 0 desactiva el aislamiento: se pide confirmar aparte.

    Es la diferencia entre "este cliente ve lo suyo" y "este cliente ve todo
    el helpdesk", y por un cero de más pasa desapercibida.
    """
    print(
        "\n  ATENCIÓN: --org-id 0 crea un cliente INTERNO, que ve los tickets de\n"
        "  TODAS las organizaciones, sin aislamiento.\n"
    )
    return input("  Escribe 'si' para confirmar: ").strip().lower() == "si"


def alta(nombre: str, org_id: int, ips: str, permisos: str) -> int:
    if org_id == ORG_TODAS and not _confirmar_org_todas():
        print("Cancelado.")
        return 1

    clave = _generar_clave()
    with database.transaccion() as conexion:
        if not tabla_existe(conexion):
            print(f"ERROR: no existe la tabla {TABLA}. Correr antes:\n"
                  f"  mysql -u USUARIO -p BASE < sql/001_api_cliente.sql", file=sys.stderr)
            return 1
        if _existe(conexion, nombre):
            print(f"ERROR: ya existe un cliente '{nombre}'. Para renovarle la "
                  f"clave: rotar --nombre {nombre}", file=sys.stderr)
            return 1

        conexion.execute(
            text(f"""
                INSERT INTO {TABLA}
                    (nombre, clave_hash, org_id, ips_permitidas, permisos, activo, creado)
                VALUES
                    (:nombre, :hash, :org_id, :ips, :permisos, 1, NOW())
            """),
            {"nombre": nombre, "hash": hash_clave(clave), "org_id": org_id,
             "ips": ips, "permisos": permisos},
        )

    _mostrar_clave(nombre, clave, org_id, permisos)
    return 0


def rotar(nombre: str) -> int:
    clave = _generar_clave()
    with database.transaccion() as conexion:
        resultado = conexion.execute(
            text(f"UPDATE {TABLA} SET clave_hash = :hash WHERE nombre = :nombre"),
            {"hash": hash_clave(clave), "nombre": nombre},
        )
        if not resultado.rowcount:
            print(f"ERROR: no existe el cliente '{nombre}'", file=sys.stderr)
            return 1
        fila = conexion.execute(
            text(f"SELECT org_id, permisos FROM {TABLA} WHERE nombre = :nombre"),
            {"nombre": nombre},
        ).first()

    _mostrar_clave(nombre, clave, int(fila.org_id), fila.permisos)
    print("  La clave anterior deja de servir en cuanto venza la caché.")
    return 0


def baja(nombre: str) -> int:
    with database.transaccion() as conexion:
        resultado = conexion.execute(
            text(f"UPDATE {TABLA} SET activo = 0 WHERE nombre = :nombre"),
            {"nombre": nombre},
        )
    if not resultado.rowcount:
        print(f"ERROR: no existe el cliente '{nombre}'", file=sys.stderr)
        return 1
    # Baja lógica: la fila queda, así que se conserva el rastro de quién era
    # y desde cuándo no se usaba.
    print(f"Cliente '{nombre}' dado de baja. Deja de tener acceso en cuanto "
          f"venza la caché.")
    return 0


def listar() -> int:
    with database.engine.connect() as conexion:
        if not tabla_existe(conexion):
            print(f"No existe la tabla {TABLA}: correr sql/001_api_cliente.sql",
                  file=sys.stderr)
            return 1
        filas = conexion.execute(
            text(f"""
                SELECT nombre, org_id, permisos, ips_permitidas, activo,
                       creado, ultimo_uso
                FROM {TABLA} ORDER BY activo DESC, nombre
            """)
        ).all()

    if not filas:
        print("No hay clientes dados de alta.")
        return 0

    print(f"{'NOMBRE':<20} {'ORG':>5}  {'ACTIVO':<7} {'PERMISOS':<18} "
          f"{'ÚLTIMO USO':<20} IPS")
    print("-" * 100)
    for f in filas:
        org = "TODAS" if f.org_id == ORG_TODAS else str(f.org_id)
        print(f"{f.nombre:<20} {org:>5}  {'sí' if f.activo else 'NO':<7} "
              f"{f.permisos:<18} {str(f.ultimo_uso or 'nunca'):<20} "
              f"{f.ips_permitidas or '-'}")
    return 0


def _mostrar_clave(nombre: str, clave: str, org_id: int, permisos: str) -> None:
    print("\n" + "=" * 72)
    print(f"  Cliente:      {nombre}")
    print(f"  Organización: {'TODAS (interno, sin aislamiento)' if org_id == ORG_TODAS else org_id}")
    print(f"  Permisos:     {permisos}")
    print(f"\n  X-API-Key:    {clave}")
    print("\n  Anotarla ahora: no se guarda en ninguna parte y no se puede")
    print("  recuperar. Si se pierde, se rota.")
    print("=" * 72 + "\n")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gestionar_clientes",
        description="Clientes de la API de osTicket: alta, baja, rotación y listado.",
    )
    sub = parser.add_subparsers(dest="accion", required=True)

    p_alta = sub.add_parser("alta", help="Da de alta un cliente y genera su clave")
    p_alta.add_argument("--nombre", required=True,
                        help="Identificador del consumidor: 'intranet', 'portal-rrhh'")
    p_alta.add_argument("--org-id", type=int, required=True,
                        help="ost_organization.id cuyos tickets podrá ver. "
                             "0 = interno, ve TODO (pide confirmación)")
    p_alta.add_argument("--ips", default="",
                        help="IPs o rangos CIDR separados por coma. Vacío = sin "
                             "filtro propio (rige solo IPS_PERMITIDAS del .env)")
    p_alta.add_argument("--permisos", default=PERMISOS_POR_DEFECTO,
                        help=f"Separados por coma. Por defecto '{PERMISOS_POR_DEFECTO}'. "
                             f"Agregar 'notas' SOLO a consumidores internos: habilita "
                             f"ver las notas internas de los agentes")

    p_rotar = sub.add_parser("rotar", help="Genera una clave nueva para un cliente")
    p_rotar.add_argument("--nombre", required=True)

    p_baja = sub.add_parser("baja", help="Revoca el acceso de un cliente")
    p_baja.add_argument("--nombre", required=True)

    sub.add_parser("listar", help="Muestra los clientes dados de alta")

    args = parser.parse_args(argv)

    if args.accion == "alta":
        return alta(args.nombre, args.org_id, args.ips, args.permisos)
    if args.accion == "rotar":
        return rotar(args.nombre)
    if args.accion == "baja":
        return baja(args.nombre)
    return listar()


if __name__ == "__main__":
    sys.exit(main())
