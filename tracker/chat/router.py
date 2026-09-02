"""
El router: agarra la operacion que devolvio el interprete y la ejecuta.

Es el unico lugar donde se decide "esta intencion se traduce en estos asientos"
y devuelve, ademas, el texto que hay que mandarle a la persona.

Todo lo que necesita saber viene por parametro (la conexion, la operacion, el
contexto del reply). No habla con Telegram ni con Claude: eso lo hace el job,
que es quien tiene la plomeria. Asi el router se puede probar sin red.
"""

from dataclasses import dataclass
from decimal import Decimal

import psycopg

from tracker import fechas
from tracker.chat import mensajes
from tracker.interpreter.claude import Operacion
from tracker.store import (
    asientos,
    fondos,
    inversiones,
    pendientes,
    reservas,
)
from tracker.store.asientos import OperacionInvalida
from tracker.store.reglas import calcular_interes, porcentaje_a_tna, redondear

#: Tipos de asiento que se pueden corregir directamente. Modificar una reserva
#: o un plazo fijo toca mas cosas que un monto, asi que se pide anular y
#: volver a cargar.
TIPOS_MODIFICABLES = frozenset({"gasto", "ingreso"})


@dataclass
class Respuesta:
    """
    Lo que hay que contestarle a la persona, y a que quedo atada la respuesta.

    El job, despues de mandar el mensaje, usa `asiento_id` / `pendiente_id`
    para guardar el message_id de Telegram. Ese guardado es lo que despues
    permite corregir por reply.
    """

    texto: str
    asiento_id: int | None = None
    pendiente_id: int | None = None


# ---------------------------------------------------------------------------
# Entrada principal
# ---------------------------------------------------------------------------

def ejecutar(
    conn: psycopg.Connection,
    operacion: Operacion,
    *,
    telegram_message_id: int | None = None,
    asiento_referido: dict | None = None,
    pendiente: dict | None = None,
) -> Respuesta:
    """
    Ejecuta una operacion y devuelve que contestar.

    `asiento_referido` es la operacion a la que el usuario le hizo reply, si
    hubo. `pendiente` es la pregunta que el bot habia dejado abierta.
    """
    manejador = _MANEJADORES.get(operacion.nombre)
    if manejador is None:
        return Respuesta(texto=mensajes.error(f"operacion desconocida: {operacion.nombre}"))

    try:
        return manejador(
            conn,
            operacion,
            telegram_message_id=telegram_message_id,
            asiento_referido=asiento_referido,
            pendiente=pendiente,
        )
    except OperacionInvalida as problema:
        # Errores esperables de las reglas del libro (anular dos veces, gastar
        # contra una reserva ya cerrada). Se los contamos a la persona en
        # castellano en vez de dejar que reviente el job.
        return Respuesta(texto=mensajes.error(str(problema)))


# ---------------------------------------------------------------------------
# Operaciones de registro
# ---------------------------------------------------------------------------

def _registrar_gasto(conn, op, *, telegram_message_id=None, **_) -> Respuesta:
    fondo = fondos.obtener_o_crear(conn, op.arg("fondo"))
    monto = redondear(op.args["monto"])
    descripcion = op.arg("descripcion", "gasto")
    categoria = op.arg("categoria")
    fecha = fechas.parsear(op.arg("fecha"))

    # Si la persona dio a entender que este gasto va contra una reserva
    # ("pague la prepaga"), buscamos cual.
    concepto_reserva = op.arg("reserva")
    if concepto_reserva:
        candidatas = reservas.buscar_activa_por_concepto(conn, concepto_reserva)

        if len(candidatas) == 1:
            return _consumir_reserva(
                conn,
                candidatas[0]["id"],
                {
                    "monto": str(monto),
                    "descripcion": descripcion,
                    "categoria": categoria,
                    "fecha": fecha.isoformat() if fecha else None,
                },
                telegram_message_id=telegram_message_id,
            )

        if len(candidatas) > 1:
            return _preguntar_cual(
                conn,
                accion="descontar",
                opciones=[
                    {"id": r["id"], "texto": f"{r['concepto']} ({mensajes.plata(r['monto'])})"}
                    for r in candidatas
                ],
                datos={
                    "accion": "consumir_reserva",
                    "monto": str(monto),
                    "descripcion": descripcion,
                    "categoria": categoria,
                    "fecha": fecha.isoformat() if fecha else None,
                },
            )
        # Si no hay ninguna reserva con ese concepto, seguimos de largo y lo
        # cargamos como un gasto comun.

    asiento = asientos.crear(
        conn,
        fondo_id=fondo["id"],
        monto=-monto,
        tipo="gasto",
        descripcion=descripcion,
        categoria=categoria,
        fecha=fecha,
        telegram_message_id=telegram_message_id,
    )
    return Respuesta(
        texto=mensajes.gasto_registrado(
            monto, descripcion, fondo["nombre"], asientos.saldo(conn, fondo["id"])
        ),
        asiento_id=asiento["id"],
    )


def _registrar_ingreso(conn, op, *, telegram_message_id=None, **_) -> Respuesta:
    fondo = fondos.obtener_o_crear(conn, op.arg("fondo"))
    monto = redondear(op.args["monto"])
    descripcion = op.arg("descripcion", "ingreso")

    asiento = asientos.crear(
        conn,
        fondo_id=fondo["id"],
        monto=monto,
        tipo="ingreso",
        descripcion=descripcion,
        categoria=op.arg("categoria"),
        fecha=fechas.parsear(op.arg("fecha")),
        telegram_message_id=telegram_message_id,
    )
    return Respuesta(
        texto=mensajes.ingreso_registrado(
            monto, descripcion, fondo["nombre"], asientos.saldo(conn, fondo["id"])
        ),
        asiento_id=asiento["id"],
    )


def _crear_reserva(conn, op, *, telegram_message_id=None, **_) -> Respuesta:
    fondo = fondos.obtener_o_crear(conn, op.arg("fondo"))
    monto = redondear(op.args["monto"])
    concepto = op.args["concepto"]

    _, asiento = reservas.crear(
        conn,
        fondo_id=fondo["id"],
        concepto=concepto,
        monto=monto,
        telegram_message_id=telegram_message_id,
    )
    return Respuesta(
        texto=mensajes.reserva_creada(
            monto, concepto, fondo["nombre"], asientos.saldo(conn, fondo["id"])
        ),
        asiento_id=asiento["id"],
    )


def _cancelar_reserva(conn, op, *, telegram_message_id=None, **_) -> Respuesta:
    candidatas = reservas.buscar_activa_por_concepto(conn, op.args["concepto"])

    if not candidatas:
        return Respuesta(
            texto=mensajes.error(
                f"no encontre una reserva activa de '{op.args['concepto']}'"
            )
        )
    if len(candidatas) > 1:
        return _preguntar_cual(
            conn,
            accion="cancelar",
            opciones=[
                {"id": r["id"], "texto": f"{r['concepto']} ({mensajes.plata(r['monto'])})"}
                for r in candidatas
            ],
            datos={"accion": "cancelar_reserva"},
        )

    return _ejecutar_cancelacion_reserva(
        conn, candidatas[0]["id"], telegram_message_id=telegram_message_id
    )


def _crear_inversion(conn, op, *, telegram_message_id=None, **_) -> Respuesta:
    fondo = fondos.obtener_o_crear(conn, op.arg("fondo"))
    capital = redondear(op.args["capital"])
    # Claude devuelve el porcentaje tal cual lo dijo la persona (35);
    # adentro se guarda como fraccion (0.35).
    tna = porcentaje_a_tna(op.args["tna_porcentaje"])
    plazo_dias = int(op.args["plazo_dias"])
    fecha_inicio = fechas.parsear(op.arg("fecha")) or fechas.hoy()

    inversion, asiento = inversiones.crear(
        conn,
        fondo_id=fondo["id"],
        capital=capital,
        tna=tna,
        plazo_dias=plazo_dias,
        fecha_inicio=fecha_inicio,
        telegram_message_id=telegram_message_id,
    )
    return Respuesta(
        texto=mensajes.inversion_creada(
            capital,
            tna,
            plazo_dias,
            inversion["fecha_vencimiento"],
            calcular_interes(capital, tna, plazo_dias),
            fondo["nombre"],
        ),
        asiento_id=asiento["id"],
    )


# ---------------------------------------------------------------------------
# Anular y modificar
# ---------------------------------------------------------------------------

def _anular_operacion(
    conn, op, *, telegram_message_id=None, asiento_referido=None, **_
) -> Respuesta:
    objetivo, alternativa = _elegir_asiento(conn, op, asiento_referido, accion="anular")
    if alternativa is not None:
        return alternativa
    return _ejecutar_anulacion(conn, objetivo, telegram_message_id=telegram_message_id)


def _modificar_operacion(
    conn, op, *, telegram_message_id=None, asiento_referido=None, **_
) -> Respuesta:
    objetivo, alternativa = _elegir_asiento(conn, op, asiento_referido, accion="corregir")
    if alternativa is not None:
        return alternativa

    cambios = {
        "monto": op.arg("monto_nuevo"),
        "descripcion": op.arg("descripcion_nueva"),
        "categoria": op.arg("categoria_nueva"),
        "fondo": op.arg("fondo_nuevo"),
    }
    return _ejecutar_modificacion(
        conn, objetivo, cambios, telegram_message_id=telegram_message_id
    )


def _elegir_asiento(conn, op, asiento_referido, *, accion):
    """
    Decide sobre que asiento actuar.

    Devuelve (asiento, None) si esta claro, o (None, Respuesta) si hay que
    contestarle algo a la persona (no lo encontre, o hay varios candidatos).

    Un reply es la via mas confiable: no hay ambiguedad posible. Sin reply,
    buscamos por descripcion.
    """
    if asiento_referido is not None:
        return asiento_referido, None

    referencia = op.arg("referencia")
    if not referencia:
        return None, Respuesta(
            texto=mensajes.error(
                "no se a cual te referis. Respondeme al mensaje de esa "
                "operacion, o deci de que era."
            )
        )

    candidatos = asientos.buscar_por_descripcion(conn, referencia)
    if not candidatos:
        return None, Respuesta(
            texto=mensajes.error(f"no encontre ninguna operacion de '{referencia}'")
        )
    if len(candidatos) == 1:
        return candidatos[0], None

    datos = {"accion": "anular" if accion == "anular" else "modificar"}
    if accion != "anular":
        datos["cambios"] = {
            "monto": _a_str(op.arg("monto_nuevo")),
            "descripcion": op.arg("descripcion_nueva"),
            "categoria": op.arg("categoria_nueva"),
            "fondo": op.arg("fondo_nuevo"),
        }
    return None, _preguntar_cual(
        conn,
        accion=accion,
        opciones=[
            {"id": a["id"], "texto": mensajes.describir_asiento(a)} for a in candidatos
        ],
        datos=datos,
    )


def _ejecutar_anulacion(conn, asiento, *, telegram_message_id=None) -> Respuesta:
    """
    Anula un asiento y limpia lo que ese asiento haya abierto.

    La reversion ya devuelve la plata sola. Lo que falta es cerrar la reserva o
    el plazo fijo que el asiento original habia creado, para que no queden
    figurando como activos.
    """
    reversion = asientos.revertir(
        conn, asiento["id"], telegram_message_id=telegram_message_id
    )

    if asiento["tipo"] == "reserva_apartada" and asiento["reserva_id"]:
        reservas.cerrar(conn, asiento["reserva_id"], "cancelada")
    elif asiento["tipo"] == "inversion_capital" and asiento["inversion_id"]:
        inversiones.cancelar(conn, asiento["inversion_id"])

    fondo = fondos.obtener_por_id(conn, asiento["fondo_id"])
    return Respuesta(
        texto=mensajes.operacion_anulada(
            asiento, asientos.saldo(conn, fondo["id"]), fondo["nombre"]
        ),
        asiento_id=reversion["id"],
    )


def _ejecutar_modificacion(conn, asiento, cambios, *, telegram_message_id=None) -> Respuesta:
    """
    Corregir = anular el asiento viejo y escribir uno nuevo con los datos bien.

    Nunca se hace UPDATE sobre el original: el historial tiene que mostrar que
    hubo una correccion.
    """
    if asiento["tipo"] not in TIPOS_MODIFICABLES:
        return Respuesta(
            texto=mensajes.error(
                f"un asiento de tipo '{asiento['tipo']}' no se corrige directo. "
                "Anulalo y volve a cargarlo."
            )
        )

    asientos.revertir(
        conn,
        asiento["id"],
        descripcion=f"Correccion de: {asiento['descripcion'] or asiento['tipo']}",
        telegram_message_id=telegram_message_id,
    )

    if cambios.get("fondo"):
        fondo = fondos.obtener_o_crear(conn, cambios["fondo"])
    else:
        fondo = fondos.obtener_por_id(conn, asiento["fondo_id"])

    # El monto nuevo viene siempre positivo; le devolvemos el signo que tenia
    # el original (un gasto sigue siendo un gasto).
    if cambios.get("monto") is not None:
        magnitud = abs(redondear(cambios["monto"]))
        monto = -magnitud if asiento["monto"] < 0 else magnitud
    else:
        monto = asiento["monto"]

    nuevo = asientos.crear(
        conn,
        fondo_id=fondo["id"],
        monto=monto,
        tipo=asiento["tipo"],
        descripcion=cambios.get("descripcion") or asiento["descripcion"],
        categoria=cambios.get("categoria") or asiento["categoria"],
        fecha=asiento["fecha"],
        canal=asiento["canal"],
        telegram_message_id=telegram_message_id,
    )
    return Respuesta(
        texto=mensajes.operacion_modificada(
            asiento, nuevo, fondo["nombre"], asientos.saldo(conn, fondo["id"])
        ),
        asiento_id=nuevo["id"],
    )


def _ejecutar_cancelacion_reserva(conn, reserva_id, *, telegram_message_id=None) -> Respuesta:
    reserva = reservas.obtener(conn, reserva_id)
    _, asiento = reservas.cancelar(
        conn, reserva_id, telegram_message_id=telegram_message_id
    )
    fondo = fondos.obtener_por_id(conn, reserva["fondo_id"])
    return Respuesta(
        texto=mensajes.reserva_cancelada(
            reserva["monto"],
            reserva["concepto"],
            fondo["nombre"],
            asientos.saldo(conn, fondo["id"]),
        ),
        asiento_id=asiento["id"],
    )


def _consumir_reserva(conn, reserva_id, datos, *, telegram_message_id=None, canal="telegram") -> Respuesta:
    """Gasta contra una reserva. `datos` viene del router o de un pendiente."""
    reserva = reservas.obtener(conn, reserva_id)
    resultado = reservas.consumir(
        conn,
        reserva_id,
        Decimal(datos["monto"]),
        descripcion=datos.get("descripcion"),
        categoria=datos.get("categoria"),
        fecha=fechas.parsear(datos.get("fecha")),
        canal=canal,
        origen_ref=datos.get("origen_ref"),
        telegram_message_id=telegram_message_id,
    )
    fondo = fondos.obtener_por_id(conn, reserva["fondo_id"])
    return Respuesta(
        texto=mensajes.reserva_consumida(
            resultado["plan"],
            reserva["concepto"],
            fondo["nombre"],
            asientos.saldo(conn, fondo["id"]),
        ),
        asiento_id=resultado["asiento_gasto"]["id"],
    )


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------

def _consultar(conn, op, **_) -> Respuesta:
    que = op.arg("que", "todo")

    if que == "reservas":
        return Respuesta(texto=mensajes.lista_reservas(reservas.listar_activas(conn)))
    if que == "inversiones":
        return Respuesta(
            texto=mensajes.lista_inversiones(inversiones.listar_activas(conn))
        )
    if que == "movimientos":
        return Respuesta(texto=mensajes.lista_movimientos(asientos.ultimos(conn)))

    resumen = asientos.resumen(conn)
    if op.arg("fondo"):
        pedido = op.arg("fondo").strip().lower()
        resumen = [f for f in resumen if f["nombre"] == pedido] or resumen

    return Respuesta(
        texto=mensajes.resumen_general(
            resumen,
            reservas.listar_activas(conn) if que == "todo" else [],
            inversiones.listar_activas(conn) if que == "todo" else [],
        )
    )


def _no_entendido(conn, op, **_) -> Respuesta:
    return Respuesta(texto=mensajes.no_entendido(op.arg("motivo", "no se que quisiste decir")))


# ---------------------------------------------------------------------------
# Pendientes
# ---------------------------------------------------------------------------

def _preguntar_cual(conn, *, accion: str, opciones: list[dict], datos: dict) -> Respuesta:
    """
    Deja una pregunta abierta con varias opciones y no toca ningun saldo.

    Cuando la persona conteste con un numero, resolver_pendiente() retoma
    exactamente desde aca gracias a lo que guardamos en el payload.
    """
    pendiente = pendientes.crear(
        conn,
        tipo="desambiguacion",
        payload={"opciones": opciones, **datos},
    )
    return Respuesta(
        texto=mensajes.pedir_desambiguacion(accion, [o["texto"] for o in opciones]),
        pendiente_id=pendiente["id"],
    )


def _responder_pendiente(conn, op, *, telegram_message_id=None, pendiente=None, **_) -> Respuesta:
    if pendiente is None:
        return Respuesta(
            texto=mensajes.error("no tengo ninguna pregunta abierta esperando respuesta")
        )
    return resolver_pendiente(
        conn,
        pendiente,
        confirma=bool(op.arg("confirma", True)),
        opcion=op.arg("opcion"),
        telegram_message_id=telegram_message_id,
    )


def resolver_pendiente(
    conn: psycopg.Connection,
    pendiente: dict,
    *,
    confirma: bool,
    opcion: int | None = None,
    telegram_message_id: int | None = None,
) -> Respuesta:
    """
    Aplica la respuesta a una pregunta que el bot habia dejado abierta.

    Es publica porque tambien la usa el job de Mercado Pago cuando la persona
    contesta si o no a un match propuesto.
    """
    payload = pendiente["payload"]

    if pendiente["tipo"] == "match_reserva":
        respuesta = _resolver_match_reserva(
            conn, payload, confirma, telegram_message_id=telegram_message_id
        )
    elif pendiente["tipo"] == "desambiguacion":
        respuesta = _resolver_desambiguacion(
            conn, payload, confirma, opcion, telegram_message_id=telegram_message_id
        )
    else:
        return Respuesta(texto=mensajes.error(f"pendiente desconocido: {pendiente['tipo']}"))

    pendientes.cerrar(conn, pendiente["id"], "resuelto")
    return respuesta


def _resolver_match_reserva(conn, payload, confirma, *, telegram_message_id=None) -> Respuesta:
    """
    "Ese pago de Mercado Pago, lo descuento de la reserva de la prepaga?"

    Si dice que si, se consume la reserva. Si dice que no, el pago igual
    existio: se registra como un gasto comun.
    """
    if confirma:
        return _consumir_reserva(
            conn,
            payload["reserva_id"],
            payload,
            telegram_message_id=telegram_message_id,
            canal="mercadopago",
        )

    fondo = fondos.obtener_o_crear(conn, payload.get("fondo"))
    monto = Decimal(payload["monto"])
    asiento = asientos.crear(
        conn,
        fondo_id=fondo["id"],
        monto=-monto,
        tipo="gasto",
        descripcion=payload.get("descripcion", "pago de Mercado Pago"),
        categoria=payload.get("categoria"),
        fecha=fechas.parsear(payload.get("fecha")),
        canal="mercadopago",
        origen_ref=payload.get("origen_ref"),
        telegram_message_id=telegram_message_id,
    )
    return Respuesta(
        texto=mensajes.pago_registrado_sin_reserva(
            payload.get("descripcion", "pago"),
            monto,
            fondo["nombre"],
            asientos.saldo(conn, fondo["id"]),
        ),
        asiento_id=asiento["id"],
    )


def _resolver_desambiguacion(conn, payload, confirma, opcion, *, telegram_message_id=None) -> Respuesta:
    """La persona eligio una de las opciones que le listamos."""
    if not confirma:
        return Respuesta(texto="Listo, no hago nada entonces.")

    opciones = payload.get("opciones", [])
    if opcion is None or not (1 <= opcion <= len(opciones)):
        return Respuesta(
            texto=mensajes.error(
                f"necesito el numero de la opcion (del 1 al {len(opciones)})"
            )
        )

    elegido = opciones[opcion - 1]["id"]
    accion = payload["accion"]

    if accion == "anular":
        return _ejecutar_anulacion(
            conn, asientos.obtener(conn, elegido), telegram_message_id=telegram_message_id
        )
    if accion == "modificar":
        return _ejecutar_modificacion(
            conn,
            asientos.obtener(conn, elegido),
            payload.get("cambios", {}),
            telegram_message_id=telegram_message_id,
        )
    if accion == "cancelar_reserva":
        return _ejecutar_cancelacion_reserva(
            conn, elegido, telegram_message_id=telegram_message_id
        )
    if accion == "consumir_reserva":
        return _consumir_reserva(
            conn, elegido, payload, telegram_message_id=telegram_message_id
        )

    return Respuesta(texto=mensajes.error(f"accion pendiente desconocida: {accion}"))


def _a_str(valor):
    """Los Decimal no entran en un JSON; los guardamos como texto."""
    return None if valor is None else str(valor)


#: Una entrada por cada tool de interpreter/tools.py.
_MANEJADORES = {
    "registrar_gasto": _registrar_gasto,
    "registrar_ingreso": _registrar_ingreso,
    "crear_reserva": _crear_reserva,
    "cancelar_reserva": _cancelar_reserva,
    "crear_inversion": _crear_inversion,
    "anular_operacion": _anular_operacion,
    "modificar_operacion": _modificar_operacion,
    "responder_pendiente": _responder_pendiente,
    "consultar": _consultar,
    "no_entendido": _no_entendido,
}
