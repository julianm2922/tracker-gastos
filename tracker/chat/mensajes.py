"""
Como se le habla al usuario por Telegram.

Todo el texto que ve la persona se arma aca, para que el router se ocupe solo
de mover plata y no se mezclen las dos cosas.

Los mensajes usan formato HTML de Telegram (<b>negrita</b>), que es mas
tolerante que el Markdown para textos con simbolos como $ o _.
"""

from tracker.store.reglas import a_decimal, tna_a_porcentaje


def plata(monto) -> str:
    """
    Formatea un monto a la argentina: $1.234.567,89

    Python formatea con la coma como separador de miles, asi que hacemos el
    intercambio a mano: primero coma -> X, despues punto -> coma, X -> punto.
    """
    monto = a_decimal(monto)
    signo = "-" if monto < 0 else ""
    entero = f"{abs(monto):,.2f}"
    entero = entero.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{signo}${entero}"


def porcentaje(tna) -> str:
    """
    Muestra una TNA guardada como fraccion (0.35) en porcentaje ("35").

    Saca los ceros de mas pero sin caer en notacion cientifica: normalize()
    sola convertiria 100.00 en 1E+2, que en un mensaje queda espantoso.
    """
    return format(tna_a_porcentaje(tna).normalize(), "f")


def _saldo_de(resumen_fondo: dict) -> str:
    linea = f"<b>{resumen_fondo['nombre']}</b>: {plata(resumen_fondo['saldo'])}"
    if resumen_fondo["comprometido"] > 0:
        total = a_decimal(resumen_fondo["saldo"]) + a_decimal(resumen_fondo["comprometido"])
        linea += (
            f" (+ {plata(resumen_fondo['comprometido'])} reservado"
            f" = {plata(total)})"
        )
    return linea


def gasto_registrado(monto, descripcion, fondo, saldo) -> str:
    texto = f"Anotado: gasto de {plata(monto)} en {descripcion} ({fondo}).\n"
    texto += f"Te quedan {plata(saldo)} en {fondo}."
    if a_decimal(saldo) < 0:
        texto += "\n\nOjo que ese fondo quedo en negativo."
    return texto


def ingreso_registrado(monto, descripcion, fondo, saldo) -> str:
    return (
        f"Anotado: ingreso de {plata(monto)} por {descripcion} ({fondo}).\n"
        f"Ahora hay {plata(saldo)} en {fondo}."
    )


def reserva_creada(monto, concepto, fondo, saldo) -> str:
    return (
        f"Aparte {plata(monto)} para {concepto} en {fondo}.\n"
        f"Disponible en {fondo}: {plata(saldo)}."
    )


def reserva_cancelada(monto, concepto, fondo, saldo) -> str:
    return (
        f"Cancele la reserva de {concepto}: vuelven {plata(monto)} a {fondo}.\n"
        f"Disponible en {fondo}: {plata(saldo)}."
    )


def reserva_consumida(plan, concepto, fondo, saldo) -> str:
    """Mensaje de los tres casos de consumo de una reserva."""
    texto = (
        f"Pague {plata(plan.monto_gasto)} de {concepto} "
        f"contra la reserva de {plata(plan.monto_reserva)}.\n"
    )
    if plan.hay_excedente:
        texto += (
            f"Se paso {plata(plan.excedente)} de lo reservado, "
            f"esa diferencia salio de {fondo}.\n"
        )
    elif plan.sobrante > 0:
        texto += f"Sobraron {plata(plan.sobrante)}, que vuelven a {fondo}.\n"
    texto += f"Disponible en {fondo}: {plata(saldo)}."
    return texto


def inversion_creada(capital, tna, plazo_dias, vencimiento, interes_estimado, fondo) -> str:
    return (
        f"Plazo fijo armado: {plata(capital)} de {fondo} al "
        f"{porcentaje(tna)}% a {plazo_dias} dias.\n"
        f"Vence el {vencimiento}, deberias cobrar "
        f"{plata(a_decimal(capital) + a_decimal(interes_estimado))} "
        f"({plata(interes_estimado)} de interes)."
    )


def inversion_acreditada(resultado, fondo, saldo) -> str:
    inversion = resultado["inversion"]
    return (
        f"Vencio el plazo fijo de {plata(inversion['capital'])}.\n"
        f"Acredite {plata(resultado['total'])} en {fondo} "
        f"({plata(resultado['interes'])} de interes).\n"
        f"Disponible en {fondo}: {plata(saldo)}."
    )


def operacion_anulada(asiento, saldo, fondo) -> str:
    return (
        f"Anule: {asiento['tipo']} de {plata(asiento['monto'])} "
        f"({asiento.get('descripcion') or 'sin descripcion'}).\n"
        f"Disponible en {fondo}: {plata(saldo)}."
    )


def operacion_modificada(anterior, nuevo, fondo, saldo) -> str:
    return (
        f"Corregido: era {plata(anterior['monto'])}, ahora es "
        f"{plata(nuevo['monto'])} ({nuevo.get('descripcion') or 'sin descripcion'}).\n"
        f"Disponible en {fondo}: {plata(saldo)}."
    )


def resumen_general(fondos_resumen, reservas, inversiones) -> str:
    if not fondos_resumen:
        return "Todavia no hay nada cargado."

    lineas = ["<b>Como venis</b>", ""]
    lineas += [_saldo_de(f) for f in fondos_resumen]

    if reservas:
        lineas += ["", "<b>Reservas activas</b>"]
        lineas += [
            f"- {r['concepto']}: {plata(r['monto'])} ({r['fondo']})" for r in reservas
        ]

    if inversiones:
        lineas += ["", "<b>Plazos fijos</b>"]
        lineas += [
            f"- {plata(i['capital'])} al {porcentaje(i['tna'])}%, "
            f"vence el {i['fecha_vencimiento']}"
            for i in inversiones
        ]

    return "\n".join(lineas)


def lista_reservas(reservas) -> str:
    if not reservas:
        return "No tenes ninguna reserva activa."
    lineas = ["<b>Reservas activas</b>"]
    lineas += [
        f"- {r['concepto']}: {plata(r['monto'])} ({r['fondo']})" for r in reservas
    ]
    return "\n".join(lineas)


def lista_inversiones(inversiones) -> str:
    if not inversiones:
        return "No tenes plazos fijos en curso."
    lineas = ["<b>Plazos fijos en curso</b>"]
    for i in inversiones:
        lineas.append(
            f"- {plata(i['capital'])} al {porcentaje(i['tna'])}% "
            f"a {i['plazo_dias']} dias, vence el {i['fecha_vencimiento']} ({i['fondo']})"
        )
    return "\n".join(lineas)


def lista_movimientos(asientos) -> str:
    if not asientos:
        return "No hay movimientos cargados todavia."
    lineas = ["<b>Ultimos movimientos</b>"]
    for a in asientos:
        lineas.append(
            f"- {a['fecha']} {plata(a['monto'])} "
            f"{a.get('descripcion') or a['tipo']} ({a['fondo']})"
        )
    return "\n".join(lineas)


def describir_asiento(asiento) -> str:
    """Una linea para identificar una operacion en una lista de opciones."""
    return (
        f"{asiento['fecha']} {plata(asiento['monto'])} "
        f"{asiento.get('descripcion') or asiento['tipo']}"
    )


def pedir_desambiguacion(accion: str, opciones: list[str]) -> str:
    lineas = [f"Hay mas de una operacion que puede ser. Cual queres {accion}?", ""]
    lineas += [f"{i}. {texto}" for i, texto in enumerate(opciones, start=1)]
    lineas.append("")
    lineas.append("Respondeme con el numero.")
    return "\n".join(lineas)


def preguntar_match_reserva(descripcion, monto, concepto_reserva, monto_reserva) -> str:
    return (
        f"Apareció un pago en Mercado Pago: <b>{descripcion}</b> por "
        f"{plata(monto)}.\n\n"
        f"Parece la reserva de <b>{concepto_reserva}</b> "
        f"({plata(monto_reserva)}). La descuento de ahi?\n\n"
        f"Respondeme si o no a este mensaje."
    )


def pago_registrado_sin_reserva(descripcion, monto, fondo, saldo) -> str:
    return (
        f"Anote el pago de Mercado Pago: {descripcion} por {plata(monto)} ({fondo}).\n"
        f"Disponible en {fondo}: {plata(saldo)}."
    )


def no_entendido(motivo: str) -> str:
    return f"No me quedo claro: {motivo}\nProba de nuevo diciendome monto y de que fue."


def error(detalle: str) -> str:
    return f"No pude hacer eso: {detalle}"
