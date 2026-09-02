"""
El interprete: convierte lo que dijo la persona en una operacion estructurada.

Le mandamos a Claude el mensaje (texto o foto) junto con un poco de contexto
(fecha de hoy, fondos que existen, reservas activas, la operacion a la que
esta respondiendo) y lo obligamos a contestar llamando a una de las tools de
tools.py. Nunca leemos prosa libre: si Claude escribe texto en vez de llamar a
una tool, para nosotros es un error.

El modelo es Haiku porque la tarea es chica (clasificar una frase en una de
diez operaciones) y es el mas barato de la familia.
"""

import base64
from dataclasses import dataclass, field
from datetime import date

import anthropic

from tracker import config
from tracker.interpreter.tools import TOOLS, TOOL_MATCH_RESERVA


class InterpretacionFallida(Exception):
    """Claude no devolvio una operacion utilizable."""


@dataclass
class Operacion:
    """Lo que devolvio el interprete: que quiere hacer la persona."""

    nombre: str
    #: Los argumentos, tal como los definio el schema de la tool.
    args: dict = field(default_factory=dict)

    def arg(self, clave: str, por_defecto=None):
        """
        Lee un argumento opcional.

        Claude a veces manda un string vacio en vez de omitir el campo, asi que
        eso tambien cuenta como "no vino".
        """
        valor = self.args.get(clave)
        if valor is None or valor == "":
            return por_defecto
        return valor


@dataclass
class Contexto:
    """
    Lo que el interprete necesita saber ademas del mensaje.

    Todo esto se arma consultando el store antes de llamar a Claude.
    """

    hoy: date
    fondos: list[str] = field(default_factory=list)
    reservas_activas: list[dict] = field(default_factory=list)
    inversiones_activas: list[dict] = field(default_factory=list)
    #: Operacion a la que el usuario le esta respondiendo (correccion por reply).
    asiento_referido: dict | None = None
    #: Pregunta del bot que el usuario esta contestando.
    pendiente: dict | None = None


def _cliente() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.anthropic_api_key())


def armar_system_prompt(contexto: Contexto) -> str:
    """
    Arma las instrucciones del sistema con el estado actual de las cuentas.

    Meter el contexto aca (y no en el mensaje del usuario) deja bien separado
    lo que dice el sistema de lo que dice la persona.
    """
    partes = [
        "Sos el interprete de un sistema personal de finanzas de una sola "
        "persona, en Argentina. Recibis un mensaje suyo (texto, o la foto de "
        "un comprobante) y tenes que traducirlo a UNA operacion, llamando a "
        "una de las tools disponibles.",
        "",
        "Reglas:",
        "- Los montos son en pesos argentinos. 'luca' o 'mil' es 1000: "
        "'12 lucas' son 12000.",
        "- Si no se aclara de que fondo sale la plata, omitir el campo 'fondo' "
        "(el sistema usa 'sueldo' por defecto).",
        "- Si no se menciona una fecha, omitir el campo 'fecha' (se usa hoy).",
        "- Ante la duda entre inventar un dato y no entender, usar "
        "'no_entendido'. Es preferible preguntar.",
        "",
        f"Hoy es {contexto.hoy.isoformat()}.",
    ]

    if contexto.fondos:
        partes.append("Fondos que ya existen: " + ", ".join(contexto.fondos) + ".")

    if contexto.reservas_activas:
        partes.append("Reservas activas (plata ya apartada):")
        for reserva in contexto.reservas_activas:
            partes.append(
                f"  - id {reserva['id']}: {reserva['concepto']}, "
                f"${reserva['monto']} en el fondo {reserva.get('fondo', '?')}"
            )

    if contexto.inversiones_activas:
        partes.append("Plazos fijos en curso:")
        for inversion in contexto.inversiones_activas:
            partes.append(
                f"  - id {inversion['id']}: ${inversion['capital']} al "
                f"{inversion['tna']} anual, vence el {inversion['fecha_vencimiento']}"
            )

    if contexto.asiento_referido:
        asiento = contexto.asiento_referido
        partes += [
            "",
            "El mensaje es una respuesta a esta operacion ya registrada:",
            f"  id {asiento['id']}: {asiento['tipo']} de ${asiento['monto']}, "
            f"'{asiento.get('descripcion') or 'sin descripcion'}', "
            f"del {asiento['fecha']}.",
            "Si la persona corrige o anula algo, es esta operacion. No hace "
            "falta que completes el campo 'referencia'.",
        ]

    if contexto.pendiente:
        pendiente = contexto.pendiente
        partes += [
            "",
            f"Hay una pregunta del bot esperando respuesta (tipo "
            f"{pendiente['tipo']}): {pendiente['payload']}.",
            "Si el mensaje la contesta, usa 'responder_pendiente'.",
        ]

    return "\n".join(partes)


def _bloque_imagen(imagen: bytes, media_type: str) -> dict:
    """Arma el bloque de contenido de una foto para mandarsela a Claude."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(imagen).decode("utf-8"),
        },
    }


def interpretar(
    texto: str | None = None,
    *,
    contexto: Contexto,
    imagen: bytes | None = None,
    media_type: str = "image/jpeg",
    cliente: anthropic.Anthropic | None = None,
) -> Operacion:
    """
    Traduce un mensaje a una operacion.

    `texto` y/o `imagen`: se puede mandar solo texto, solo una foto, o las dos
    cosas (una foto con epigrafe). Las fotos van directo como imagen, no hay
    OCR ni parsers: Claude las lee.

    `cliente` existe para poder inyectar un doble en los tests.
    """
    if texto is None and imagen is None:
        raise InterpretacionFallida("No hay ni texto ni imagen para interpretar")

    contenido: list[dict] = []
    if imagen is not None:
        contenido.append(_bloque_imagen(imagen, media_type))
        if not texto:
            texto = (
                "Este es el comprobante de una operacion. Registrala con lo "
                "que puedas leer de la imagen."
            )
    contenido.append({"type": "text", "text": texto})

    cliente = cliente or _cliente()
    respuesta = cliente.messages.create(
        model=config.MODELO_CLAUDE,
        max_tokens=1024,
        system=armar_system_prompt(contexto),
        tools=TOOLS,
        # "any" obliga a llamar a alguna tool: nunca vamos a tener que parsear
        # prosa. disable_parallel_tool_use garantiza que sea una sola, asi un
        # mensaje = una operacion.
        tool_choice={"type": "any", "disable_parallel_tool_use": True},
        messages=[{"role": "user", "content": contenido}],
    )

    for bloque in respuesta.content:
        if bloque.type == "tool_use":
            return Operacion(nombre=bloque.name, args=dict(bloque.input))

    raise InterpretacionFallida(
        f"Claude no llamo a ninguna tool (stop_reason={respuesta.stop_reason})"
    )


def matchear_reserva(
    descripcion_pago: str,
    monto_pago,
    reservas_activas: list[dict],
    *,
    cliente: anthropic.Anthropic | None = None,
) -> dict:
    """
    Decide si un pago de Mercado Pago corresponde a alguna reserva activa.

    Es el paso semantico del flujo de Mercado Pago: un pago a "GALENO SA" tiene
    que reconocerse como la reserva "prepaga". Devuelve el diccionario que
    definio TOOL_MATCH_RESERVA (reserva_id puede ser None).

    Ojo: esto NO ejecuta nada. Quien llama tiene que crear un pendiente y
    preguntarle a la persona antes de tocar un saldo.
    """
    lineas = [
        f"Pago a interpretar: '{descripcion_pago}' por ${monto_pago}.",
        "",
        "Reservas activas:",
    ]
    if reservas_activas:
        for reserva in reservas_activas:
            lineas.append(
                f"  - id {reserva['id']}: {reserva['concepto']} (${reserva['monto']})"
            )
    else:
        lineas.append("  (ninguna)")

    cliente = cliente or _cliente()
    respuesta = cliente.messages.create(
        model=config.MODELO_CLAUDE,
        max_tokens=512,
        system=(
            "Sos parte de un sistema personal de finanzas. Te paso un pago que "
            "aparecio en Mercado Pago y las reservas de plata que la persona "
            "tenia apartadas. Decidi si el pago corresponde a alguna."
        ),
        tools=[TOOL_MATCH_RESERVA],
        tool_choice={"type": "tool", "name": TOOL_MATCH_RESERVA["name"]},
        messages=[{"role": "user", "content": "\n".join(lineas)}],
    )

    for bloque in respuesta.content:
        if bloque.type == "tool_use":
            return dict(bloque.input)

    raise InterpretacionFallida("Claude no evaluo el match del pago")
