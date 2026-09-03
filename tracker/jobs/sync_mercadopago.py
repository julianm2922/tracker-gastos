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
pedir el reporte a MP no lo devuelve al toque, encola una "tarea" que segun la
documentacion tarda "desde segundos hasta varios minutos" en terminar. En la
practica se observo una tardando casi 2 horas, asi que esa promesa no es muy
confiable. Este job espera un poco (hasta MAX_ESPERA_SEGUNDOS, corto a
proposito) por si la tarea termina rapido, pero el mecanismo que de verdad
sostiene esto es otro:

- Si no hay ninguna tarea pendiente, se pide un reporte nuevo.
- Se espera un rato corto a que esa tarea (la nueva, o una pendiente de una
  corrida anterior) termine. Si termina, se procesa en la misma corrida.
- Si no termina en ese rato, no se pierde nada: el id de la tarea queda
  guardado en estado_app y la corrida de mañana la retoma justo donde quedo,
  sin pedir una tarea nueva de mas. Esto, y no la espera corta, es lo que
  realmente garantiza que el reporte se termine procesando tarde o temprano.
- Si una tarea lleva mas de MAX_DIAS_ESPERANDO_TAREA dias sin terminar
  (esperando entre corridas, no en una sola espera), se la abandona y se pide
  una nueva: algo raro paso y insistirle a MP con la misma tarea para siempre
  no tiene sentido.

Se piden DIAS_HACIA_ATRAS dias de movimientos, no solo el ultimo: si una
corrida falla o el reporte se demora, la siguiente igual cubre lo que falto.
Los repetidos no molestan porque el dedupe por origen_ref los filtra.
"""

import time
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

# Cuanto esperamos, como maximo, a que la tarea de MP termine EN ESTA CORRIDA.
#
# En la practica, un reporte de varios dias puede tardar bastante mas que "unos
# minutos" (la documentacion de MP promete eso, pero se observo una tarea real
# tardando casi 2 horas en terminar). Por eso esta espera es corta a proposito:
# no tiene sentido quemar minutos del runner en una espera que casi nunca va a
# alcanzar. El mecanismo que de verdad resuelve esto es que el id de la tarea
# queda guardado y la corrida de mañana la retoma sola (ver el docstring del
# modulo); esta espera solo esta para el caso ocasional en que el reporte
# termina rapido y se puede procesar en el momento.
MAX_ESPERA_SEGUNDOS = 3 * 60
INTERVALO_CONSULTA_SEGUNDOS = 20


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


def _esperar_tarea(tarea_id) -> dict:
    """
    Consulta el estado de la tarea cada INTERVALO_CONSULTA_SEGUNDOS, hasta que
    termine o hasta MAX_ESPERA_SEGUNDOS, lo que pase primero.

    No es una espera indefinida a proposito: si MP tarda mas que eso, es mejor
    cortar y dejar que la corrida de mañana la retome (el id sigue guardado)
    que arriesgarse a que el workflow entero se corte por timeout a mitad de
    algo.
    """
    limite = time.monotonic() + MAX_ESPERA_SEGUNDOS
    tarea = mercadopago.consultar_tarea(tarea_id)
    intentos = 1

    while tarea.get("status") != "processed" and time.monotonic() < limite:
        time.sleep(INTERVALO_CONSULTA_SEGUNDOS)
        tarea = mercadopago.consultar_tarea(tarea_id)
        intentos += 1

    print(f"Tarea {tarea_id}: status={tarea.get('status')} (consultada {intentos} vez/veces)")
    return tarea


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

    tarea = _esperar_tarea(tarea_id)

    if tarea.get("status") != "processed":
        print(
            f"La tarea {tarea_id} no termino despues de esperar "
            f"{MAX_ESPERA_SEGUNDOS}s (ultimo estado: {tarea.get('status')}). "
            f"Se retoma en la proxima corrida."
        )
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
