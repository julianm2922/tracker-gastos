"""
Fondos: "sueldo", "ahorro", "bono", los que hagan falta.

Se pueden crear en cualquier momento. De hecho, si el usuario menciona un
fondo que no existe, `obtener_o_crear` lo crea solo.
"""

import psycopg

from tracker import config
from tracker.store.db import filas, una_fila


def normalizar(nombre: str) -> str:
    """
    Nombre canonico de un fondo: minusculas y sin espacios de mas.

    Asi "Ahorro", "ahorro " y "AHORRO" son todos el mismo fondo.
    """
    return nombre.strip().lower()


def obtener_por_nombre(conn: psycopg.Connection, nombre: str) -> dict | None:
    """Busca un fondo por nombre. Devuelve None si no existe."""
    return una_fila(
        conn, "SELECT * FROM fondos WHERE nombre = %s", (normalizar(nombre),)
    )


def obtener_por_id(conn: psycopg.Connection, fondo_id: int) -> dict | None:
    return una_fila(conn, "SELECT * FROM fondos WHERE id = %s", (fondo_id,))


def obtener_o_crear(conn: psycopg.Connection, nombre: str | None = None) -> dict:
    """
    Devuelve el fondo pedido, creandolo si no existia.

    Si `nombre` es None se usa el fondo por defecto ("sueldo"), que es la regla
    para las operaciones donde el usuario no aclara de donde sale la plata.
    """
    nombre = normalizar(nombre or config.FONDO_POR_DEFECTO)

    fondo = obtener_por_nombre(conn, nombre)
    if fondo is not None:
        return fondo

    # ON CONFLICT por si dos corridas intentan crear el mismo fondo a la vez.
    conn.execute(
        "INSERT INTO fondos (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING",
        (nombre,),
    )
    return obtener_por_nombre(conn, nombre)


def listar(conn: psycopg.Connection) -> list[dict]:
    return filas(conn, "SELECT * FROM fondos ORDER BY nombre")
