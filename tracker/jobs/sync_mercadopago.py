"""
Flujo B: importar los movimientos de Mercado Pago y acreditar plazos fijos.

Lo dispara el cron una vez por dia:

    python -m tracker.jobs.sync_mercadopago

Hace dos cosas, en este orden:

1. Acredita los plazos fijos que hayan vencido y avisa (ya esta hecho, no
   pregunta nada).
2. Trae los movimientos de MP, descarta los que ya estaban, y para cada uno
   decide si lo registra o si pregunta antes (cuando parece corresponder a una
   reserva).

Se piden DIAS_HACIA_ATRAS dias de movimientos, no solo el ultimo: si una
corrida falla, la siguiente recupera lo que falto. Los repetidos no molestan
porque el dedupe por origen_ref los filtra.
"""

import traceback
from datetime import timedelta

from tracker import config, fechas
from tracker.chat import mensajes, telegram
from tracker.movements import mercadopago, sync
from tracker.store import asientos, db, fondos, inversiones, pendientes

DIAS_HACIA_ATRAS = 7


def acreditar_vencidos(conn, chat_id: int) -> None:
    """Acredita capital + interes de los plazos fijos vencidos y avisa."""
    for inversion in inversiones.listar_vencidas(conn, fechas.hoy()):
        resultado = inversiones.acreditar(conn, inversion["id"])
        fondo = fondos.obtener_por_id(conn, inversion["fondo_id"])

        telegram.enviar_mensaje(
            chat_id,
            mensajes.inversion_acreditada(
                resultado, fondo["nombre"], asientos.saldo(conn, fondo["id"])
            ),
        )
        conn.commit()


def importar_movimientos(conn, chat_id: int) -> None:
    """Trae los movimientos de MP y los procesa."""
    hasta = fechas.hoy()
    desde = hasta - timedelta(days=DIAS_HACIA_ATRAS)

    movimientos = mercadopago.obtener_movimientos(desde, hasta)
    print(f"{len(movimientos)} movimientos en el reporte")

    resultado = sync.procesar(conn, movimientos)
    conn.commit()

    for aviso in resultado.avisos:
        telegram.enviar_mensaje(chat_id, aviso)

    # Las preguntas van al final y guardamos el message_id de cada una, para
    # que la persona pueda contestarlas con un reply.
    for pregunta in resultado.preguntas:
        enviado = telegram.enviar_mensaje(chat_id, pregunta["texto"])
        pendientes.marcar_mensaje_del_bot(
            conn, pregunta["pendiente_id"], enviado["message_id"]
        )
        conn.commit()

    print(
        f"registrados: {resultado.registrados}, "
        f"ya estaban: {resultado.duplicados}, "
        f"preguntas: {len(resultado.preguntas)}"
    )


def main() -> None:
    chat_id = config.telegram_allowed_chat_id()

    with db.conectar() as conn:
        # Los plazos fijos se acreditan igual aunque falle Mercado Pago: son
        # dos cosas independientes y no queremos que una arrastre a la otra.
        acreditar_vencidos(conn, chat_id)

        try:
            importar_movimientos(conn, chat_id)
        except Exception:
            conn.rollback()
            traceback.print_exc()
            telegram.enviar_mensaje(
                chat_id,
                "No pude traer los movimientos de Mercado Pago esta vez. "
                "Lo reintento en la proxima corrida.",
            )


if __name__ == "__main__":
    main()
