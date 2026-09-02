"""
Conexion a Postgres.

Usamos psycopg 3 con SQL a mano, sin ORM. Para un proyecto de este tamaño el
ORM esconde mas de lo que ayuda, y ver el SQL de cerca es parte del punto.

Convencion del proyecto: ninguna funcion del store abre su propia conexion.
Todas reciben `conn` como primer argumento. Quien manda (los jobs) abre una
conexion, hace lo suyo y la cierra. Asi varias operaciones pueden compartir
una misma transaccion.
"""

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from tracker import config

# Ruta al archivo con el esquema, al lado de este modulo.
RUTA_SCHEMA = Path(__file__).parent / "schema.sql"


def conectar(url: str | None = None) -> psycopg.Connection:
    """
    Abre una conexion a Postgres.

    `row_factory=dict_row` hace que cada fila venga como diccionario
    (fila["monto"]) en vez de como tupla (fila[3]), que se lee mucho mejor.

    Se usa como context manager:

        with conectar() as conn:
            ...

    Al salir del `with` psycopg hace COMMIT si no hubo excepcion, o ROLLBACK
    si la hubo. Es justo lo que queremos: si un job falla a la mitad, no queda
    medio asiento escrito.
    """
    return psycopg.connect(url or config.database_url(), row_factory=dict_row)


def aplicar_schema(conn: psycopg.Connection) -> None:
    """
    Corre schema.sql. Es idempotente: se puede correr todas las veces que sea.

    Esta es toda nuestra "migracion inicial". Cuando haya que cambiar algo, se
    agrega al final del archivo con IF NOT EXISTS.
    """
    conn.execute(RUTA_SCHEMA.read_text(encoding="utf-8"))


def una_fila(conn: psycopg.Connection, sql: str, params: tuple = ()) -> dict | None:
    """Ejecuta una consulta y devuelve la primera fila, o None si no hay."""
    return conn.execute(sql, params).fetchone()


def filas(conn: psycopg.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Ejecuta una consulta y devuelve todas las filas."""
    return conn.execute(sql, params).fetchall()
