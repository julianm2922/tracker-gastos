"""
Flujo B: importar los movimientos de Mercado Pago y acreditar plazos fijos.

Tiene dos alcances, que se piden como argumento de linea de comandos:

    python -m tracker.jobs.sync_mercadopago diario     # todos los dias
    python -m tracker.jobs.sync_mercadopago mensual    # una vez por semana

- "diario" pide un reporte de los ultimos DIAS_VENTANA_DIARIA dias (una
  ventana chica, pensada para verse al dia).
- "mensual" pide el mes actual completo, como revision de respaldo: agarra
  cualquier cosa que el sync diario se haya perdido (una tarea abandonada, un
  movimiento que MP ajusto despues de generado el reporte diario, etc).

Los dos alcances son completamente independientes entre si: cada uno sigue su
propia tarea en estado_app, bajo una clave distinta, asi que corren sin
pisarse. Y como el registro dedupe por origen_ref, que el mismo movimiento
aparezca en el reporte diario y despues de nuevo en el mensual no genera un
duplicado.

Ademas de importar movimientos, el job acredita los plazos fijos que hayan
vencido y avisa (ya esta hecho, no pregunta nada) — esto se hace siempre,
en las dos corridas, independientemente del alcance.

Sobre el reporte de MP, un detalle que vale la pena explicar porque no es
obvio: pedirlo no lo devuelve al toque, encola una "tarea" que segun la
documentacion tarda "desde segundos hasta varios minutos" en terminar. En la
practica se observo una tardando casi 2 horas, asi que esa promesa no es muy
confiable — y no hay evidencia de que pedir un rango de fechas mas chico la
acelere; el "diario" existe sobre todo como buena practica (menos para
procesar, mas frecuente, mejor pista de que paso) y no porque este garantizado
que sea mas rapido. El job espera un poco (hasta MAX_ESPERA_SEGUNDOS, corto a
proposito) por si la tarea termina rapido, pero el mecanismo que de verdad
sostiene esto es otro:

- Si no hay ninguna tarea pendiente para este alcance, se pide un reporte
  nuevo.
- Se espera un rato corto a que esa tarea (la nueva, o una pendiente de una
  corrida anterior del mismo alcance) termine. Si termina, se procesa en la
  misma corrida.
- Si no termina en ese rato, no se pierde nada: el id de la tarea queda
  guardado en estado_app y la proxima corrida de ese alcance la retoma justo
  donde quedo, sin pedir una tarea nueva de mas. Esto, y no la espera corta,
  es lo que realmente garantiza que el reporte se termine procesando tarde o
  temprano.
- Si una tarea lleva mas de MAX_DIAS_ESPERANDO_TAREA dias sin terminar
  (esperando entre corridas, no en una sola espera), se la abandona y se pide
  una nueva: algo raro paso y insistirle a MP con la misma tarea para siempre
  no tiene sentido.
"""

import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from tracker import config, fechas
from tracker.chat import mensajes, telegram
from tracker.movements import mercadopago, sync
from tracker.store import asientos, db, estado, fondos, inversiones, pendientes

# Cuantos dias hacia atras pide el alcance "diario". Mas de uno a proposito:
# si el cron corrio temprano en la madrugada, el dia de ayer recien esta
# terminando de asentarse del lado de MP, y con solo "hoy" se corre el riesgo
# de pedir un reporte casi vacio y perderse el cierre de ayer.
DIAS_VENTANA_DIARIA = 2

MAX_DIAS_ESPERANDO_TAREA = 5

# Cuanto esperamos, como maximo, a que la tarea de MP termine EN ESTA CORRIDA.
#
# En la practica, un reporte puede tardar bastante mas que "unos minutos" (la
# documentacion de MP promete eso, pero se observo una tarea real tardando
# casi 2 horas en terminar). Por eso esta espera es corta a proposito: no
# tiene sentido quemar minutos del runner en una espera que casi nunca va a
# alcanzar. El mecanismo que de verdad resuelve esto es que el id de la tarea
# queda guardado y la proxima corrida de este alcance la retoma sola (ver el
# docstring del modulo); esta espera solo esta para el caso ocasional en que
# el reporte termina rapido y se puede procesar en el momento.
MAX_ESPERA_SEGUNDOS = 3 * 60
INTERVALO_CONSULTA_SEGUNDOS = 20


def _rango_diario() -> tuple[date, date]:
    hasta = fechas.hoy()
    return hasta - timedelta(days=DIAS_VENTANA_DIARIA - 1), hasta


def _rango_mensual() -> tuple[date, date]:
    hoy = fechas.hoy()
    return hoy.replace(day=1), hoy


@dataclass(frozen=True)
class Alcance:
    """
    Que rango de fechas pedir y bajo que claves de estado_app seguir la tarea.

    Cada alcance es independiente: "diario" y "mensual" nunca se pisan porque
    usan claves distintas. `rango` es una funcion (no un metodo) porque las
    dos variantes solo difieren en eso; no hace falta una subclase por cada
    una.
    """

    nombre: str
    clave_tarea_id: str
    clave_tarea_fecha: str
    rango: Callable[[], tuple[date, date]]


ALCANCE_DIARIO = Alcance(
    nombre="diario",
    clave_tarea_id="mp_reporte_diario_tarea_id",
    clave_tarea_fecha="mp_reporte_diario_tarea_fecha",
    rango=_rango_diario,
)
ALCANCE_MENSUAL = Alcance(
    nombre="mensual",
    clave_tarea_id="mp_reporte_mensual_tarea_id",
    clave_tarea_fecha="mp_reporte_mensual_tarea_fecha",
    rango=_rango_mensual,
)
ALCANCES = {a.nombre: a for a in (ALCANCE_DIARIO, ALCANCE_MENSUAL)}


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


def _tarea_pendiente(conn, alcance: Alcance) -> str | None:
    """
    El id de una tarea de este alcance, de una corrida anterior, que todavia
    no se proceso, o None.

    Si lleva demasiados dias sin terminar, la abandona (borra el registro) y
    devuelve None, como si no hubiera ninguna: algo se trabo del lado de MP y
    seguir preguntando por la misma tarea para siempre no tiene sentido.
    """
    tarea_id = estado.obtener(conn, alcance.clave_tarea_id)
    if tarea_id is None:
        return None

    pedida_el = fechas.parsear(estado.obtener(conn, alcance.clave_tarea_fecha))
    if pedida_el and (fechas.hoy() - pedida_el).days > MAX_DIAS_ESPERANDO_TAREA:
        print(
            f"[{alcance.nombre}] La tarea {tarea_id} lleva mas de "
            f"{MAX_DIAS_ESPERANDO_TAREA} dias sin terminar (pedida el "
            f"{pedida_el}). La abandono y pido una nueva."
        )
        _limpiar_tarea(conn, alcance)
        return None

    return tarea_id


def _guardar_tarea(conn, alcance: Alcance, tarea_id) -> None:
    estado.guardar(conn, alcance.clave_tarea_id, str(tarea_id))
    estado.guardar(conn, alcance.clave_tarea_fecha, fechas.hoy().isoformat())


def _limpiar_tarea(conn, alcance: Alcance) -> None:
    estado.borrar(conn, alcance.clave_tarea_id)
    estado.borrar(conn, alcance.clave_tarea_fecha)


def _esperar_tarea(tarea_id) -> dict:
    """
    Consulta el estado de la tarea cada INTERVALO_CONSULTA_SEGUNDOS, hasta que
    termine o hasta MAX_ESPERA_SEGUNDOS, lo que pase primero.

    No es una espera indefinida a proposito: si MP tarda mas que eso, es mejor
    cortar y dejar que la proxima corrida de este alcance la retome (el id
    sigue guardado) que arriesgarse a que el workflow entero se corte por
    timeout a mitad de algo.
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


def importar_movimientos(conn, chat_id: int, alcance: Alcance) -> None:
    """
    Sigue el estado del reporte de MP para este alcance y, cuando esta listo,
    procesa los movimientos. Ver el docstring del modulo para la logica de la
    tarea.
    """
    tarea_id = _tarea_pendiente(conn, alcance)

    if tarea_id is None:
        desde, hasta = alcance.rango()
        tarea = mercadopago.pedir_reporte(desde, hasta)
        tarea_id = tarea["id"]
        _guardar_tarea(conn, alcance, tarea_id)
        conn.commit()
        print(f"[{alcance.nombre}] Reporte pedido ({desde} a {hasta}). Tarea {tarea_id}.")
    else:
        print(f"[{alcance.nombre}] Seguia pendiente la tarea {tarea_id} de una corrida anterior.")

    tarea = _esperar_tarea(tarea_id)

    if tarea.get("status") != "processed":
        print(
            f"[{alcance.nombre}] La tarea {tarea_id} no termino despues de "
            f"esperar {MAX_ESPERA_SEGUNDOS}s (ultimo estado: {tarea.get('status')}). "
            f"Se retoma en la proxima corrida."
        )
        return

    nombre_archivo = tarea["file_name"]
    movimientos = mercadopago.normalizar_csv(mercadopago.descargar_reporte(nombre_archivo))
    print(f"[{alcance.nombre}] {len(movimientos)} movimientos en {nombre_archivo}")

    resultado = sync.procesar(conn, movimientos)
    # Se libera el "lugar" para que la proxima corrida de este alcance pida un
    # reporte nuevo. Si algo de lo anterior tira una excepcion, no llegamos
    # hasta aca: la tarea sigue guardada y la proxima corrida retoma esta
    # misma, sin pedir una tarea nueva de mas.
    _limpiar_tarea(conn, alcance)
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
        f"[{alcance.nombre}] registrados: {resultado.registrados}, "
        f"ya estaban: {resultado.duplicados}, "
        f"preguntas: {len(resultado.preguntas)}"
    )


def main() -> None:
    nombre_alcance = sys.argv[1] if len(sys.argv) > 1 else "diario"
    alcance = ALCANCES.get(nombre_alcance)
    if alcance is None:
        opciones = ", ".join(ALCANCES)
        sys.exit(f"Alcance desconocido: {nombre_alcance!r}. Opciones: {opciones}")

    chat_id = config.telegram_allowed_chat_id()

    with db.conectar() as conn:
        # Los plazos fijos se acreditan igual aunque falle Mercado Pago: son
        # dos cosas independientes y no queremos que una arrastre a la otra.
        # Se hace en las dos corridas (diaria y mensual); acreditar dos veces
        # el mismo vencimiento no puede pasar, porque una vez acreditado el
        # plazo fijo deja de aparecer en listar_vencidas().
        acreditar_vencidos(conn, chat_id)

        try:
            importar_movimientos(conn, chat_id, alcance)
        except Exception:
            conn.rollback()
            traceback.print_exc()
            telegram.enviar_mensaje(
                chat_id,
                f"No pude traer los movimientos de Mercado Pago esta vez "
                f"(sync {alcance.nombre}). Lo reintento en la proxima corrida.",
            )


if __name__ == "__main__":
    main()
