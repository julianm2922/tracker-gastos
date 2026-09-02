"""
Reservas: plata apartada dentro de un fondo para un gasto que ya sabemos que
viene ("la prepaga", "el alquiler").

Ciclo de vida:

    activa ---consumir---> consumida
      |
      +------cancelar----> cancelada

Apartar plata genera un asiento negativo, asi que la reserva sale del saldo
disponible pero sigue contando en el total del fondo.
"""

from datetime import date

import psycopg

from tracker.store import asientos
from tracker.store.asientos import OperacionInvalida
from tracker.store.db import filas, una_fila
from tracker.store.reglas import planificar_consumo_reserva, redondear


def crear(
    conn: psycopg.Connection,
    *,
    fondo_id: int,
    concepto: str,
    monto,
    fecha: date | None = None,
    telegram_message_id: int | None = None,
) -> tuple[dict, dict]:
    """
    Aparta plata: crea la reserva y su asiento negativo.

    Devuelve (reserva, asiento).
    """
    monto = redondear(monto)
    if monto <= 0:
        raise OperacionInvalida("Una reserva tiene que ser de un monto positivo")

    reserva = una_fila(
        conn,
        """
        INSERT INTO reservas (fondo_id, concepto, monto)
        VALUES (%s, %s, %s)
        RETURNING *
        """,
        (fondo_id, concepto.strip(), monto),
    )

    asiento = asientos.crear(
        conn,
        fondo_id=fondo_id,
        monto=-monto,
        tipo="reserva_apartada",
        descripcion=f"Reserva para {concepto}",
        fecha=fecha,
        reserva_id=reserva["id"],
        telegram_message_id=telegram_message_id,
    )
    return reserva, asiento


def obtener(conn: psycopg.Connection, reserva_id: int) -> dict | None:
    return una_fila(conn, "SELECT * FROM reservas WHERE id = %s", (reserva_id,))


def listar_activas(conn: psycopg.Connection, fondo_id: int | None = None) -> list[dict]:
    """Reservas todavia abiertas, opcionalmente filtradas por fondo."""
    if fondo_id is None:
        return filas(
            conn,
            """
            SELECT r.*, f.nombre AS fondo
            FROM reservas r JOIN fondos f ON f.id = r.fondo_id
            WHERE r.estado = 'activa'
            ORDER BY r.creado_en
            """,
        )
    return filas(
        conn,
        """
        SELECT r.*, f.nombre AS fondo
        FROM reservas r JOIN fondos f ON f.id = r.fondo_id
        WHERE r.estado = 'activa' AND r.fondo_id = %s
        ORDER BY r.creado_en
        """,
        (fondo_id,),
    )


def buscar_activa_por_concepto(conn: psycopg.Connection, concepto: str) -> list[dict]:
    """Reservas activas cuyo concepto se parece a `concepto`."""
    return filas(
        conn,
        """
        SELECT r.*, f.nombre AS fondo
        FROM reservas r JOIN fondos f ON f.id = r.fondo_id
        WHERE r.estado = 'activa' AND r.concepto ILIKE %s
        ORDER BY r.creado_en
        """,
        (f"%{concepto.strip()}%",),
    )


def cerrar(conn: psycopg.Connection, reserva_id: int, estado: str) -> dict:
    """
    Marca la reserva como cerrada, SIN mover un peso.

    Casi siempre se llama desde cancelar() o consumir(), que ademas escriben
    los asientos que corresponden. La excepcion es cuando se anula el asiento
    que creo la reserva: ahi la plata ya vuelve sola por la reversion, y lo
    unico que falta es cerrar la reserva para que deje de figurar como activa.
    """
    return una_fila(
        conn,
        """
        UPDATE reservas
        SET estado = %s, cerrado_en = now()
        WHERE id = %s AND estado = 'activa'
        RETURNING *
        """,
        (estado, reserva_id),
    )


def cancelar(
    conn: psycopg.Connection,
    reserva_id: int,
    *,
    fecha: date | None = None,
    telegram_message_id: int | None = None,
) -> tuple[dict, dict]:
    """
    Cancela una reserva: devuelve todo al fondo y la cierra.

    Devuelve (reserva, asiento de devolucion).
    """
    reserva = obtener(conn, reserva_id)
    if reserva is None:
        raise OperacionInvalida(f"No existe la reserva {reserva_id}")
    if reserva["estado"] != "activa":
        raise OperacionInvalida(
            f"La reserva de {reserva['concepto']} ya estaba {reserva['estado']}"
        )

    asiento = asientos.crear(
        conn,
        fondo_id=reserva["fondo_id"],
        monto=reserva["monto"],
        tipo="reserva_devuelta",
        descripcion=f"Cancelacion de la reserva para {reserva['concepto']}",
        fecha=fecha,
        reserva_id=reserva_id,
        telegram_message_id=telegram_message_id,
    )
    return cerrar(conn, reserva_id, "cancelada"), asiento


def consumir(
    conn: psycopg.Connection,
    reserva_id: int,
    monto_gasto,
    *,
    descripcion: str | None = None,
    categoria: str | None = None,
    fecha: date | None = None,
    canal: str = "telegram",
    origen_ref: str | None = None,
    telegram_message_id: int | None = None,
) -> dict:
    """
    Gasta contra una reserva y la cierra.

    Escribe dos asientos (ver PlanConsumo en reglas.py): uno devolviendo toda
    la reserva al fondo y otro registrando el gasto real. El neto sobre el
    saldo es el gasto, tanto si sobro plata como si falto.

    Devuelve un diccionario con el plan y los asientos creados, para que quien
    llama pueda armar el mensaje de Telegram (y avisar si hubo excedente).
    """
    reserva = obtener(conn, reserva_id)
    if reserva is None:
        raise OperacionInvalida(f"No existe la reserva {reserva_id}")
    if reserva["estado"] != "activa":
        raise OperacionInvalida(
            f"La reserva de {reserva['concepto']} ya estaba {reserva['estado']}"
        )

    plan = planificar_consumo_reserva(reserva["monto"], monto_gasto)
    descripcion = descripcion or reserva["concepto"]

    devolucion = asientos.crear(
        conn,
        fondo_id=reserva["fondo_id"],
        monto=plan.monto_reserva,
        tipo="reserva_devuelta",
        descripcion=f"Se libera la reserva para {reserva['concepto']}",
        fecha=fecha,
        canal=canal,
        reserva_id=reserva_id,
        telegram_message_id=telegram_message_id,
    )

    gasto = asientos.crear(
        conn,
        fondo_id=reserva["fondo_id"],
        monto=-plan.monto_gasto,
        tipo="gasto",
        descripcion=descripcion,
        categoria=categoria or reserva["concepto"],
        fecha=fecha,
        canal=canal,
        reserva_id=reserva_id,
        origen_ref=origen_ref,
        telegram_message_id=telegram_message_id,
    )

    return {
        "reserva": cerrar(conn, reserva_id, "consumida"),
        "plan": plan,
        "asiento_devolucion": devolucion,
        "asiento_gasto": gasto,
    }
