"""
Asientos: el libro. Todo lo que mueve plata pasa por aca.

Reglas que este modulo hace cumplir:

- Un asiento no se modifica ni se borra nunca. Para anularlo se crea otro
  asiento con `revierte_a_id` apuntando al original.
- El monto va con signo: negativo sale del fondo, positivo entra.
- El saldo de un fondo es la suma de sus asientos. No se guarda en ningun lado.
"""

from datetime import date
from decimal import Decimal

import psycopg

from tracker.store.db import filas, una_fila
from tracker.store.reglas import redondear

# Los tipos validos estan tambien en el CHECK de schema.sql. Los repetimos aca
# para poder fallar temprano y con un mensaje entendible, en vez de que reviente
# la base con un error de constraint.
TIPOS_VALIDOS = frozenset({
    "ingreso",
    "gasto",
    "reserva_apartada",
    "reserva_devuelta",
    "inversion_capital",
    "inversion_retorno",
    "reversion",
})

CANALES_VALIDOS = frozenset({"telegram", "mercadopago", "sistema"})


class OperacionInvalida(Exception):
    """El pedido no tiene sentido segun las reglas del libro."""


def crear(
    conn: psycopg.Connection,
    *,
    fondo_id: int,
    monto,
    tipo: str,
    descripcion: str | None = None,
    categoria: str | None = None,
    fecha: date | None = None,
    canal: str = "telegram",
    reserva_id: int | None = None,
    inversion_id: int | None = None,
    revierte_a_id: int | None = None,
    origen_ref: str | None = None,
    telegram_message_id: int | None = None,
    telegram_bot_message_id: int | None = None,
) -> dict:
    """
    Escribe un asiento y lo devuelve.

    Todos los argumentos van por nombre (el `*` los fuerza) porque una llamada
    con siete numeros posicionales seria imposible de leer.
    """
    if tipo not in TIPOS_VALIDOS:
        raise OperacionInvalida(f"Tipo de asiento desconocido: {tipo!r}")
    if canal not in CANALES_VALIDOS:
        raise OperacionInvalida(f"Canal desconocido: {canal!r}")

    monto = redondear(monto)
    if monto == 0:
        raise OperacionInvalida("Un asiento de monto 0 no registra nada")

    return una_fila(
        conn,
        """
        INSERT INTO asientos (
            fondo_id, monto, tipo, descripcion, categoria, fecha, canal,
            reserva_id, inversion_id, revierte_a_id, origen_ref,
            telegram_message_id, telegram_bot_message_id
        )
        VALUES (%s, %s, %s, %s, %s, COALESCE(%s::date, CURRENT_DATE), %s,
                %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            fondo_id, monto, tipo, descripcion, categoria, fecha, canal,
            reserva_id, inversion_id, revierte_a_id, origen_ref,
            telegram_message_id, telegram_bot_message_id,
        ),
    )


def obtener(conn: psycopg.Connection, asiento_id: int) -> dict | None:
    return una_fila(conn, "SELECT * FROM asientos WHERE id = %s", (asiento_id,))


def esta_anulado(conn: psycopg.Connection, asiento_id: int) -> bool:
    """Un asiento esta anulado si existe otro que lo revierte."""
    fila = una_fila(
        conn, "SELECT 1 FROM asientos WHERE revierte_a_id = %s", (asiento_id,)
    )
    return fila is not None


def revertir(
    conn: psycopg.Connection,
    asiento_id: int,
    *,
    descripcion: str | None = None,
    telegram_message_id: int | None = None,
) -> dict:
    """
    Anula un asiento creando su espejo con el monto invertido.

    No toca el asiento original: despues de esto el fondo queda como si la
    operacion nunca hubiera pasado, pero el historial muestra las dos cosas.

    Anular una reserva o una inversion tiene consecuencias mas alla del monto
    (hay que cerrar la reserva, cancelar el plazo fijo). Eso lo resuelven
    reservas.py e inversiones.py; aca solo va el movimiento de plata.
    """
    original = obtener(conn, asiento_id)
    if original is None:
        raise OperacionInvalida(f"No existe el asiento {asiento_id}")

    if original["tipo"] == "reversion":
        raise OperacionInvalida("No se puede anular una anulacion")

    # El UNIQUE de revierte_a_id ya lo impediria, pero asi el error se entiende.
    if esta_anulado(conn, asiento_id):
        raise OperacionInvalida(f"El asiento {asiento_id} ya estaba anulado")

    if descripcion is None:
        descripcion = f"Anulacion de: {original['descripcion'] or original['tipo']}"

    return crear(
        conn,
        fondo_id=original["fondo_id"],
        monto=-original["monto"],
        tipo="reversion",
        descripcion=descripcion,
        categoria=original["categoria"],
        canal=original["canal"],
        reserva_id=original["reserva_id"],
        inversion_id=original["inversion_id"],
        revierte_a_id=asiento_id,
        telegram_message_id=telegram_message_id,
    )


# ---------------------------------------------------------------------------
# Saldos. Nada de esto esta guardado: siempre se calcula.
# ---------------------------------------------------------------------------

def saldo(conn: psycopg.Connection, fondo_id: int) -> Decimal:
    """
    Plata que realmente se puede gastar de este fondo.

    Ya descuenta reservas e inversiones, porque apartarlas genero asientos
    negativos.
    """
    fila = una_fila(
        conn,
        "SELECT COALESCE(SUM(monto), 0) AS saldo FROM asientos WHERE fondo_id = %s",
        (fondo_id,),
    )
    return redondear(fila["saldo"])


def comprometido(conn: psycopg.Connection, fondo_id: int) -> Decimal:
    """Plata apartada en reservas activas de este fondo."""
    fila = una_fila(
        conn,
        """
        SELECT COALESCE(SUM(monto), 0) AS total
        FROM reservas
        WHERE fondo_id = %s AND estado = 'activa'
        """,
        (fondo_id,),
    )
    return redondear(fila["total"])


def total(conn: psycopg.Connection, fondo_id: int) -> Decimal:
    """Saldo + comprometido: todo lo que hay en el fondo, usable o no."""
    return redondear(saldo(conn, fondo_id) + comprometido(conn, fondo_id))


def resumen(conn: psycopg.Connection) -> list[dict]:
    """
    Foto de todos los fondos, para responder un "cuanto tengo".

    Se resuelve en una sola consulta en vez de un saldo() por fondo: son dos
    subconsultas y evita el clasico problema de N+1 queries.
    """
    return filas(
        conn,
        """
        SELECT
            f.id,
            f.nombre,
            COALESCE((SELECT SUM(a.monto) FROM asientos a
                      WHERE a.fondo_id = f.id), 0) AS saldo,
            COALESCE((SELECT SUM(r.monto) FROM reservas r
                      WHERE r.fondo_id = f.id AND r.estado = 'activa'), 0)
                AS comprometido
        FROM fondos f
        ORDER BY f.nombre
        """,
    )


# ---------------------------------------------------------------------------
# Busquedas (para correcciones por reply y para el dedupe de Mercado Pago)
# ---------------------------------------------------------------------------

def buscar_por_mensaje_telegram(conn: psycopg.Connection, message_id: int) -> dict | None:
    """
    Busca el asiento ligado a un mensaje de Telegram.

    Sirve tanto si el usuario responde a su propio mensaje como si responde a
    la confirmacion del bot: probamos contra los dos ids.
    """
    return una_fila(
        conn,
        """
        SELECT * FROM asientos
        WHERE telegram_message_id = %s OR telegram_bot_message_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (message_id, message_id),
    )


def marcar_mensaje_del_bot(
    conn: psycopg.Connection, asiento_id: int, bot_message_id: int
) -> None:
    """
    Guarda el id del mensaje de confirmacion que mando el bot.

    Es la unica excepcion a "los asientos no se modifican": no cambia ni un
    peso ni una fecha, solo anota a que mensaje de Telegram quedo atado. Sin
    esto no se podrian resolver las correcciones por reply.
    """
    conn.execute(
        "UPDATE asientos SET telegram_bot_message_id = %s WHERE id = %s",
        (bot_message_id, asiento_id),
    )


def existe_origen(conn: psycopg.Connection, origen_ref: str) -> bool:
    """True si ya importamos ese movimiento de Mercado Pago."""
    fila = una_fila(
        conn, "SELECT 1 FROM asientos WHERE origen_ref = %s", (origen_ref,)
    )
    return fila is not None


def ultimos(conn: psycopg.Connection, limite: int = 10) -> list[dict]:
    """Ultimos asientos, para listarlos cuando hay que desambiguar."""
    return filas(
        conn,
        """
        SELECT a.*, f.nombre AS fondo
        FROM asientos a
        JOIN fondos f ON f.id = a.fondo_id
        WHERE a.tipo <> 'reversion'
          AND NOT EXISTS (SELECT 1 FROM asientos r WHERE r.revierte_a_id = a.id)
        ORDER BY a.id DESC
        LIMIT %s
        """,
        (limite,),
    )


def buscar_por_descripcion(
    conn: psycopg.Connection, texto: str, limite: int = 5
) -> list[dict]:
    """
    Busca asientos vigentes cuya descripcion contenga `texto`.

    Es para cuando el usuario dice "anula el de la farmacia" sin hacer reply.
    ILIKE es un LIKE que ignora mayusculas. Si vuelve mas de uno, quien llama
    tiene que preguntar cual (crear un pendiente de desambiguacion).
    """
    return filas(
        conn,
        """
        SELECT a.*, f.nombre AS fondo
        FROM asientos a
        JOIN fondos f ON f.id = a.fondo_id
        WHERE a.descripcion ILIKE %s
          AND a.tipo <> 'reversion'
          AND NOT EXISTS (SELECT 1 FROM asientos r WHERE r.revierte_a_id = a.id)
        ORDER BY a.id DESC
        LIMIT %s
        """,
        (f"%{texto}%", limite),
    )
