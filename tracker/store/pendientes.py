"""
Pendientes: preguntas que el bot hizo y todavia esperan respuesta.

Los dos casos:

- `match_reserva`: llego un pago de Mercado Pago que parece corresponder a una
  reserva activa. No tocamos ningun saldo hasta que el usuario confirme.
- `desambiguacion`: el usuario dijo "anula el de la farmacia" y hay mas de un
  candidato.

Un pendiente guarda en `payload` (JSON) todo lo necesario para ejecutar la
operacion despues, cuando llegue el "si".
"""

import psycopg
from psycopg.types.json import Jsonb

from tracker.store.db import filas, una_fila


def crear(
    conn: psycopg.Connection,
    *,
    tipo: str,
    payload: dict,
    telegram_bot_message_id: int | None = None,
) -> dict:
    """Anota una pregunta abierta. No mueve un peso."""
    return una_fila(
        conn,
        """
        INSERT INTO pendientes (tipo, payload, telegram_bot_message_id)
        VALUES (%s, %s, %s)
        RETURNING *
        """,
        (tipo, Jsonb(payload), telegram_bot_message_id),
    )


def existe_para_origen(conn: psycopg.Connection, origen_ref: str) -> bool:
    """
    True si ya hay una pregunta abierta sobre este movimiento de Mercado Pago.

    Hace falta porque un pendiente todavia no escribio ningun asiento: sin este
    chequeo, la corrida del dia siguiente volveria a encontrar el mismo pago
    "sin importar" y preguntaria de nuevo.

    `payload->>'origen_ref'` saca ese campo del JSON como texto.
    """
    fila = una_fila(
        conn,
        """
        SELECT 1 FROM pendientes
        WHERE estado = 'esperando' AND payload->>'origen_ref' = %s
        """,
        (origen_ref,),
    )
    return fila is not None


def obtener(conn: psycopg.Connection, pendiente_id: int) -> dict | None:
    return una_fila(conn, "SELECT * FROM pendientes WHERE id = %s", (pendiente_id,))


def listar_esperando(conn: psycopg.Connection) -> list[dict]:
    return filas(
        conn,
        "SELECT * FROM pendientes WHERE estado = 'esperando' ORDER BY creado_en",
    )


def buscar_por_mensaje_del_bot(
    conn: psycopg.Connection, bot_message_id: int
) -> dict | None:
    """
    Busca el pendiente atado a un mensaje que mando el bot.

    Cuando el usuario responde "si" haciendo reply a la pregunta, esto resuelve
    a que se referia sin ninguna ambiguedad.
    """
    return una_fila(
        conn,
        """
        SELECT * FROM pendientes
        WHERE telegram_bot_message_id = %s AND estado = 'esperando'
        ORDER BY id DESC LIMIT 1
        """,
        (bot_message_id,),
    )


def ultimo_esperando(conn: psycopg.Connection) -> dict | None:
    """
    El pendiente abierto mas reciente.

    Se usa cuando el usuario contesta "si" sin hacer reply: como es un solo
    usuario, lo mas razonable es asumir que responde a la ultima pregunta.
    """
    return una_fila(
        conn,
        """
        SELECT * FROM pendientes
        WHERE estado = 'esperando'
        ORDER BY id DESC LIMIT 1
        """,
    )


def marcar_mensaje_del_bot(
    conn: psycopg.Connection, pendiente_id: int, bot_message_id: int
) -> None:
    """Guarda a que mensaje de Telegram quedo atada la pregunta."""
    conn.execute(
        "UPDATE pendientes SET telegram_bot_message_id = %s WHERE id = %s",
        (bot_message_id, pendiente_id),
    )


def cerrar(conn: psycopg.Connection, pendiente_id: int, estado: str) -> dict | None:
    """Cierra un pendiente como 'resuelto' o 'descartado'."""
    if estado not in ("resuelto", "descartado"):
        raise ValueError(f"Estado invalido para un pendiente: {estado!r}")
    return una_fila(
        conn,
        """
        UPDATE pendientes SET estado = %s, cerrado_en = now()
        WHERE id = %s AND estado = 'esperando'
        RETURNING *
        """,
        (estado, pendiente_id),
    )
