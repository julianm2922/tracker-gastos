"""
Flujo B: importar los movimientos de Mercado Pago y acreditar plazos fijos.

Lo dispara el cron una vez por dia:

    python -m tracker.jobs.sync_mercadopago

Hace dos cosas, en este orden:

1. Acredita los plazos fijos que hayan vencido y avisa (ya esta hecho, no
   pregunta nada).
2. Sigue el estado del reporte de MP y, cuando esta listo, decide para cada
   movimiento si lo registra o si pregunta antes (cuando parece corresponder a
   una reserva).

Sobre el punto 2, un detalle que vale la pena explicar porque no es obvio:
pedir el reporte a MP no lo devuelve al toque, encola una "tarea" que tarda
desde segundos hasta varios minutos en terminar. Como este job corre una vez
por dia, en vez de quedarse esperando a que la tarea termine (bloqueando la
corrida, y arriesgando perderla si tarda mas de lo que esperamos), el id de la
tarea se guarda en estado_app:

- Si no hay ninguna tarea pendiente, se pide un reporte nuevo y se guarda su id.
- Si hay una pendiente de la corrida anterior, se consulta si ya termino. Si
  todavia no, no se pide una nueva (para no acumular pedidos) y se reintenta
  mañana.
- Si una tarea lleva mas de MAX_DIAS_ESPERANDO_TAREA dias sin terminar, se la
  abandona y se pide una nueva: algo raro paso y insistirle a MP con la misma
  tarea para siempre no tiene sentido.

Se piden DIAS_HACIA_ATRAS dias de movimientos, no solo el ultimo: si una
corrida falla o el reporte se demora, la siguiente igual cubre lo que falto.
Los repetidos no molestan porque el dedupe por origen_ref los filtra.
"""

import traceback
from datetime import timedelta

from tracker import config, fechas
from tracker.chat import mensajes, telegram
from tracker.movements import mercadopago, sync
from tracker.store import asientos, db, estado, fondos, inversiones, pendientes

DIAS_HACIA_ATRAS = 7

CLAVE_TAREA_ID = "mp_reporte_tarea_id"
CLAVE_TAREA_FECHA = "mp_reporte_tarea_fecha"
MAX_DIAS_ESPERANDO_TAREA = 3


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


def _tarea_pendiente(conn) -> str | None:
    """
    El id de una tarea de una corrida anterior que todavia no proceso, o None.

    Si lleva demasiados dias sin terminar, la abandona (borra el registro) y
    devuelve None, como si no hubiera ninguna: algo se trabo del lado de MP y
    seguir preguntando por la misma tarea para siempre no tiene sentido.
    """
    tarea_id = estado.obtener(conn, CLAVE_TAREA_ID)
    if tarea_id is None:
        return None

    pedida_el = fechas.parsear(estado.obtener(conn, CLAVE_TAREA_FECHA))
    if pedida_el and (fechas.hoy() - pedida_el).days > MAX_DIAS_ESPERANDO_TAREA:
        print(
            f"La tarea {tarea_id} lleva mas de {MAX_DIAS_ESPERANDO_TAREA} dias "
            f"sin terminar (pedida el {pedida_el}). La abandono y pido una nueva."
        )
        _limpiar_tarea(conn)
        return None

    return tarea_id


def _guardar_tarea(conn, tarea_id) -> None:
    estado.guardar(conn, CLAVE_TAREA_ID, str(tarea_id))
    estado.guardar(conn, CLAVE_TAREA_FECHA, fechas.hoy().isoformat())


def _limpiar_tarea(conn) -> None:
    estado.borrar(conn, CLAVE_TAREA_ID)
    estado.borrar(conn, CLAVE_TAREA_FECHA)


def importar_movimientos(conn, chat_id: int) -> None:
    """
    Sigue el estado del reporte de MP y, cuando esta listo, procesa los
    movimientos. Ver el docstring del modulo para la logica de la tarea.
    """
    tarea_id = _tarea_pendiente(conn)

    if tarea_id is None:
        hasta = fechas.hoy()
        desde = hasta - timedelta(days=DIAS_HACIA_ATRAS)
        tarea = mercadopago.pedir_reporte(desde, hasta)
        tarea_id = tarea["id"]
        _guardar_tarea(conn, tarea_id)
        conn.commit()
        print(f"Reporte pedido ({desde} a {hasta}). Tarea {tarea_id}.")
    else:
        print(f"Seguia pendiente la tarea {tarea_id} de una corrida anterior.")

    tarea = mercadopago.consultar_tarea(tarea_id)
    estado_tarea = tarea.get("status")
    print(f"Estado de la tarea {tarea_id}: {estado_tarea}")

    if estado_tarea != "processed":
        print("Todavia no esta lista. Se revisa de nuevo en la proxima corrida.")
        return

    nombre_archivo = tarea["file_name"]
    movimientos = mercadopago.normalizar_csv(mercadopago.descargar_reporte(nombre_archivo))
    print(f"{len(movimientos)} movimientos en {nombre_archivo}")

    resultado = sync.procesar(conn, movimientos)
    # Se libera el "lugar" para que la proxima corrida pida un reporte nuevo.
    # Si algo de lo anterior tira una excepcion, no llegamos hasta aca: la
    # tarea sigue guardada y la proxima corrida retoma esta misma, sin pedir
    # una tarea nueva de mas.
    _limpiar_tarea(conn)
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
