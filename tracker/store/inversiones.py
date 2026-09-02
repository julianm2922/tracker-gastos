"""
Inversiones. Por ahora solo plazos fijos.

Ciclo de vida:

    activa ---vence y el cron la acredita---> acreditada
      |
      +------se anula el asiento de capital--> cancelada

Poner plata a plazo fijo genera un asiento negativo por el capital. Cuando
vence, el cron escribe dos asientos positivos (capital e interes) en el mismo
fondo del que salio.
"""

from datetime import date

import psycopg

from tracker.store import asientos
from tracker.store.asientos import OperacionInvalida
from tracker.store.db import filas, una_fila
from tracker.store.reglas import (
    calcular_interes,
    calcular_vencimiento,
    redondear,
)


def crear(
    conn: psycopg.Connection,
    *,
    fondo_id: int,
    capital,
    tna,
    plazo_dias: int,
    fecha_inicio: date,
    telegram_message_id: int | None = None,
) -> tuple[dict, dict]:
    """
    Arma un plazo fijo. `tna` va como fraccion decimal (0.35 = 35% anual).

    Devuelve (inversion, asiento del capital).
    """
    capital = redondear(capital)
    if capital <= 0:
        raise OperacionInvalida("El capital tiene que ser positivo")

    vencimiento = calcular_vencimiento(fecha_inicio, plazo_dias)

    inversion = una_fila(
        conn,
        """
        INSERT INTO inversiones
            (fondo_id, capital, tna, plazo_dias, fecha_inicio, fecha_vencimiento)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (fondo_id, capital, tna, plazo_dias, fecha_inicio, vencimiento),
    )

    asiento = asientos.crear(
        conn,
        fondo_id=fondo_id,
        monto=-capital,
        tipo="inversion_capital",
        descripcion=f"Plazo fijo a {plazo_dias} dias",
        fecha=fecha_inicio,
        inversion_id=inversion["id"],
        telegram_message_id=telegram_message_id,
    )
    return inversion, asiento


def obtener(conn: psycopg.Connection, inversion_id: int) -> dict | None:
    return una_fila(conn, "SELECT * FROM inversiones WHERE id = %s", (inversion_id,))


def listar_activas(conn: psycopg.Connection) -> list[dict]:
    return filas(
        conn,
        """
        SELECT i.*, f.nombre AS fondo
        FROM inversiones i JOIN fondos f ON f.id = i.fondo_id
        WHERE i.estado = 'activa'
        ORDER BY i.fecha_vencimiento
        """,
    )


def listar_vencidas(conn: psycopg.Connection, hasta: date) -> list[dict]:
    """Plazos fijos activos que ya vencieron al dia `hasta`."""
    return filas(
        conn,
        """
        SELECT i.*, f.nombre AS fondo
        FROM inversiones i JOIN fondos f ON f.id = i.fondo_id
        WHERE i.estado = 'activa' AND i.fecha_vencimiento <= %s
        ORDER BY i.fecha_vencimiento
        """,
        (hasta,),
    )


def acreditar(conn: psycopg.Connection, inversion_id: int) -> dict:
    """
    Acredita un plazo fijo vencido: capital + interes vuelven a su fondo.

    Son dos asientos separados y no uno solo por el total, para poder mirar
    despues cuanto se gano de interes sin tener que restar nada.

    Lo llama el cron, no el usuario: cuando vence, ya esta hecho y solo se
    avisa por Telegram.
    """
    inversion = obtener(conn, inversion_id)
    if inversion is None:
        raise OperacionInvalida(f"No existe la inversion {inversion_id}")
    if inversion["estado"] != "activa":
        raise OperacionInvalida(
            f"El plazo fijo {inversion_id} ya estaba {inversion['estado']}"
        )

    interes = calcular_interes(
        inversion["capital"], inversion["tna"], inversion["plazo_dias"]
    )

    asiento_capital = asientos.crear(
        conn,
        fondo_id=inversion["fondo_id"],
        monto=inversion["capital"],
        tipo="inversion_retorno",
        descripcion="Devolucion del capital del plazo fijo",
        fecha=inversion["fecha_vencimiento"],
        canal="sistema",
        inversion_id=inversion_id,
    )
    asiento_interes = asientos.crear(
        conn,
        fondo_id=inversion["fondo_id"],
        monto=interes,
        tipo="inversion_retorno",
        descripcion="Intereses del plazo fijo",
        categoria="intereses",
        fecha=inversion["fecha_vencimiento"],
        canal="sistema",
        inversion_id=inversion_id,
    )

    actualizada = una_fila(
        conn,
        """
        UPDATE inversiones SET estado = 'acreditada'
        WHERE id = %s AND estado = 'activa'
        RETURNING *
        """,
        (inversion_id,),
    )

    return {
        "inversion": actualizada,
        "interes": interes,
        "total": redondear(inversion["capital"] + interes),
        "asientos": [asiento_capital, asiento_interes],
    }


def cancelar(conn: psycopg.Connection, inversion_id: int) -> dict | None:
    """
    Marca una inversion como cancelada.

    Se usa cuando se anula el asiento del capital: sin esto, el cron seguiria
    esperando el vencimiento de un plazo fijo que ya no existe.
    """
    return una_fila(
        conn,
        """
        UPDATE inversiones SET estado = 'cancelada'
        WHERE id = %s AND estado = 'activa'
        RETURNING *
        """,
        (inversion_id,),
    )
