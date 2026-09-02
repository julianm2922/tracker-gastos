"""
Memoria minima entre corridas del cron (tabla estado_app).

El caso principal es el offset de getUpdates de Telegram. Telegram te da los
mensajes nuevos a partir de un offset; si no lo guardamos en algun lado, cada
corrida del cron volveria a procesar los mismos mensajes y duplicariamos todo.
"""

import psycopg

from tracker.store.db import una_fila

CLAVE_OFFSET_TELEGRAM = "telegram_offset"


def obtener(conn: psycopg.Connection, clave: str) -> str | None:
    """Lee un valor guardado, o None si nunca se guardo."""
    fila = una_fila(conn, "SELECT valor FROM estado_app WHERE clave = %s", (clave,))
    return fila["valor"] if fila else None


def guardar(conn: psycopg.Connection, clave: str, valor: str) -> None:
    """Guarda (o pisa) un valor."""
    conn.execute(
        """
        INSERT INTO estado_app (clave, valor)
        VALUES (%s, %s)
        ON CONFLICT (clave) DO UPDATE
            SET valor = EXCLUDED.valor,
                actualizado_en = now()
        """,
        (clave, valor),
    )


def obtener_offset_telegram(conn: psycopg.Connection) -> int | None:
    """Ultimo update_id procesado + 1, o None la primera vez."""
    valor = obtener(conn, CLAVE_OFFSET_TELEGRAM)
    return int(valor) if valor is not None else None


def guardar_offset_telegram(conn: psycopg.Connection, offset: int) -> None:
    """Guarda desde donde tiene que seguir leyendo la proxima corrida."""
    guardar(conn, CLAVE_OFFSET_TELEGRAM, str(offset))
