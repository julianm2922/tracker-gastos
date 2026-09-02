"""
Que hacer con cada movimiento que llega de Mercado Pago.

El flujo por movimiento:

    ya lo importamos? (origen_ref)      -> descartar
    matchea con una reserva activa?     -> crear un pendiente y preguntar.
                                           NO se toca ningun saldo todavia.
    no matchea                          -> categorizar con Claude y registrar

La regla importante es la del medio: un match es una interpretacion, no un
hecho. Hasta que la persona no confirme, la reserva sigue activa y el gasto no
esta cargado.
"""

from dataclasses import dataclass, field

import psycopg

from tracker.chat import mensajes
from tracker.interpreter import claude
from tracker.store import asientos, fondos, pendientes, reservas


@dataclass
class ResultadoSync:
    """Resumen de una corrida, para loguear y para avisar por Telegram."""

    registrados: int = 0
    duplicados: int = 0
    preguntas: list[dict] = field(default_factory=list)
    #: Textos ya listos para mandar por Telegram.
    avisos: list[str] = field(default_factory=list)


def procesar_movimiento(
    conn: psycopg.Connection,
    movimiento: dict,
    resultado: ResultadoSync,
    *,
    cliente_claude=None,
) -> None:
    """Procesa un movimiento de MP. Ver el docstring del modulo para el flujo."""
    origen_ref = movimiento["origen_ref"]

    # Dos formas de "ya lo vimos": o quedo registrado como asiento, o hay una
    # pregunta abierta sobre el esperando respuesta.
    if asientos.existe_origen(conn, origen_ref) or pendientes.existe_para_origen(
        conn, origen_ref
    ):
        resultado.duplicados += 1
        return

    monto = movimiento["monto"]
    descripcion = movimiento["descripcion"]
    fondo = fondos.obtener_o_crear(conn)  # los pagos con MP salen del sueldo

    # Los ingresos no se matchean contra reservas: una reserva es plata
    # apartada para gastar, no para cobrar.
    if monto > 0:
        asientos.crear(
            conn,
            fondo_id=fondo["id"],
            monto=monto,
            tipo="ingreso",
            descripcion=descripcion,
            fecha=movimiento["fecha"],
            canal="mercadopago",
            origen_ref=origen_ref,
        )
        resultado.registrados += 1
        return

    gasto = abs(monto)
    activas = reservas.listar_activas(conn)

    evaluacion = claude.matchear_reserva(
        descripcion, gasto, activas, cliente=cliente_claude
    )
    categoria = evaluacion.get("categoria")
    descripcion_linda = evaluacion.get("descripcion") or descripcion

    reserva_id = evaluacion.get("reserva_id")
    if reserva_id:
        reserva = reservas.obtener(conn, reserva_id)
        # Ojo con lo que devuelve el modelo: solo aceptamos un id que exista y
        # que siga activo. Un numero inventado no puede llegar a la base.
        if reserva is not None and reserva["estado"] == "activa":
            pendiente = pendientes.crear(
                conn,
                tipo="match_reserva",
                payload={
                    "reserva_id": reserva["id"],
                    "monto": str(gasto),
                    "descripcion": descripcion_linda,
                    "categoria": categoria,
                    "fecha": movimiento["fecha"].isoformat(),
                    "origen_ref": origen_ref,
                    "fondo": fondo["nombre"],
                },
            )
            resultado.preguntas.append({
                "pendiente_id": pendiente["id"],
                "texto": mensajes.preguntar_match_reserva(
                    descripcion_linda, gasto, reserva["concepto"], reserva["monto"]
                ),
            })
            return

    asientos.crear(
        conn,
        fondo_id=fondo["id"],
        monto=-gasto,
        tipo="gasto",
        descripcion=descripcion_linda,
        categoria=categoria,
        fecha=movimiento["fecha"],
        canal="mercadopago",
        origen_ref=origen_ref,
    )
    resultado.registrados += 1
    resultado.avisos.append(
        mensajes.pago_registrado_sin_reserva(
            descripcion_linda, gasto, fondo["nombre"], asientos.saldo(conn, fondo["id"])
        )
    )


def procesar(
    conn: psycopg.Connection, movimientos: list[dict], *, cliente_claude=None
) -> ResultadoSync:
    """Procesa todos los movimientos de una corrida."""
    resultado = ResultadoSync()
    for movimiento in movimientos:
        procesar_movimiento(
            conn, movimiento, resultado, cliente_claude=cliente_claude
        )
    return resultado
