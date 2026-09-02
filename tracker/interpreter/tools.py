"""
Las "tools" que le damos a Claude para que devuelva algo estructurado.

La idea es no parsear nunca prosa libre. Le pasamos a Claude un catalogo de
operaciones posibles, cada una con su schema, y lo obligamos a elegir una
(tool_choice = "any"). Lo que vuelve es un JSON con la forma que pedimos.

Cada tool de aca se corresponde con una funcion de tracker/chat/router.py.
Si agregas una tool, agrega tambien su rama en el router.
"""

# Descripciones que se repiten, para no escribirlas cinco veces.
_FONDO = {
    "type": "string",
    "description": (
        "Nombre del fondo del que sale o al que entra la plata, por ejemplo "
        "'sueldo', 'ahorro' o 'bono'. Omitir si la persona no lo aclara: en "
        "ese caso se usa 'sueldo'."
    ),
}

_FECHA = {
    "type": "string",
    "description": (
        "Fecha de la operacion en formato AAAA-MM-DD. Omitir si la persona no "
        "menciona ninguna fecha: se usa la de hoy. Solo completarla cuando "
        "diga algo como 'ayer' o 'el 3 de marzo'."
    ),
}

_MONTO = {
    "type": "number",
    "description": (
        "Monto en pesos, positivo y sin puntos ni simbolos. "
        "'12 lucas' o '12 mil' son 12000."
    ),
}


TOOLS = [
    {
        "name": "registrar_gasto",
        "description": (
            "Registrar plata que salio. Ejemplos: 'gaste 12000 de mi sueldo en "
            "la farmacia', 'pague 250000 del ahorro al albañil'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "monto": _MONTO,
                "descripcion": {
                    "type": "string",
                    "description": "En que se gasto, en pocas palabras. Ej: 'farmacia'.",
                },
                "categoria": {
                    "type": "string",
                    "description": (
                        "Categoria del gasto, en una palabra y en minusculas: "
                        "salud, comida, transporte, servicios, hogar, ocio, "
                        "impuestos, otros."
                    ),
                },
                "fondo": _FONDO,
                "fecha": _FECHA,
                "reserva": {
                    "type": "string",
                    "description": (
                        "Concepto de la reserva contra la que se imputa este "
                        "gasto, si la persona da a entender que ya habia "
                        "apartado plata para esto. Ej: si dice 'pague la "
                        "prepaga' y existe una reserva 'prepaga', poner "
                        "'prepaga'. Omitir si no corresponde."
                    ),
                },
            },
            "required": ["monto", "descripcion"],
        },
    },
    {
        "name": "registrar_ingreso",
        "description": (
            "Registrar plata que entro. Ejemplos: 'cobre el sueldo, 1200000', "
            "'me devolvieron 5000'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "monto": _MONTO,
                "descripcion": {
                    "type": "string",
                    "description": "De donde vino la plata. Ej: 'sueldo de marzo'.",
                },
                "categoria": {
                    "type": "string",
                    "description": "Categoria del ingreso: sueldo, freelance, regalo, otros.",
                },
                "fondo": _FONDO,
                "fecha": _FECHA,
            },
            "required": ["monto", "descripcion"],
        },
    },
    {
        "name": "crear_reserva",
        "description": (
            "Apartar plata de un fondo para un gasto futuro concreto, sin "
            "gastarla todavia. Ejemplo: 'reservo 420000 para la prepaga'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "monto": _MONTO,
                "concepto": {
                    "type": "string",
                    "description": "Para que es la reserva. Ej: 'prepaga', 'alquiler'.",
                },
                "fondo": _FONDO,
            },
            "required": ["monto", "concepto"],
        },
    },
    {
        "name": "cancelar_reserva",
        "description": (
            "Deshacer una reserva sin haberla gastado: la plata vuelve al "
            "fondo. Ejemplo: 'cancela la reserva de la prepaga'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "concepto": {
                    "type": "string",
                    "description": (
                        "Concepto de la reserva a cancelar, tal como aparece "
                        "en la lista de reservas activas del contexto."
                    ),
                },
            },
            "required": ["concepto"],
        },
    },
    {
        "name": "crear_inversion",
        "description": (
            "Poner plata a plazo fijo. Ejemplo: 'puse 500000 a plazo fijo al "
            "35% a 30 dias'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "capital": {
                    "type": "number",
                    "description": "Plata que se invierte, en pesos.",
                },
                "tna_porcentaje": {
                    "type": "number",
                    "description": (
                        "Tasa nominal anual COMO PORCENTAJE, tal cual la dijo "
                        "la persona: para 'al 35%' poner 35, no 0.35."
                    ),
                },
                "plazo_dias": {
                    "type": "integer",
                    "description": "Duracion del plazo fijo en dias.",
                },
                "fondo": _FONDO,
                "fecha": _FECHA,
            },
            "required": ["capital", "tna_porcentaje", "plazo_dias"],
        },
    },
    {
        "name": "anular_operacion",
        "description": (
            "Borrar una operacion que estuvo mal cargada. Ejemplos: 'anula "
            "eso', 'ese gasto de la farmacia no va'. Si el mensaje es una "
            "respuesta a otro mensaje, el contexto ya trae la operacion "
            "senalada y no hace falta describirla."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "referencia": {
                    "type": "string",
                    "description": (
                        "Como identifico la persona la operacion, si no vino "
                        "senalada por el contexto. Ej: 'el de la farmacia'."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "modificar_operacion",
        "description": (
            "Corregir una operacion ya cargada. Ejemplo, respondiendo al "
            "mensaje de un gasto: 'che, eran 15000 no 12000'. Internamente se "
            "anula la vieja y se crea una nueva, pero eso no hace falta "
            "explicarlo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "referencia": {
                    "type": "string",
                    "description": (
                        "Como identifico la persona la operacion, si no vino "
                        "senalada por el contexto."
                    ),
                },
                "monto_nuevo": {
                    "type": "number",
                    "description": "Monto corregido, si lo que cambia es la plata.",
                },
                "descripcion_nueva": {
                    "type": "string",
                    "description": "Descripcion corregida, si lo que cambia es esa.",
                },
                "categoria_nueva": {
                    "type": "string",
                    "description": "Categoria corregida, si lo que cambia es esa.",
                },
                "fondo_nuevo": {
                    "type": "string",
                    "description": "Fondo corregido, si la plata salia de otro lado.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "responder_pendiente",
        "description": (
            "La persona esta contestando una pregunta que hizo el bot. Usar "
            "SOLO si el contexto trae una pregunta pendiente. Ejemplos de "
            "respuesta: 'si', 'dale', 'no', 'no era esa'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirma": {
                    "type": "boolean",
                    "description": "true si acepta lo que propuso el bot, false si lo rechaza.",
                },
                "opcion": {
                    "type": "integer",
                    "description": (
                        "Si el bot ofrecio una lista numerada de opciones, "
                        "cual eligio (empezando en 1)."
                    ),
                },
            },
            "required": ["confirma"],
        },
    },
    {
        "name": "consultar",
        "description": (
            "La persona pregunta como esta parada, no registra nada. "
            "Ejemplos: 'cuanto tengo?', 'que reservas tengo activas?', "
            "'como viene el plazo fijo?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "que": {
                    "type": "string",
                    "enum": ["saldo", "reservas", "inversiones", "movimientos", "todo"],
                    "description": (
                        "Que quiere saber. 'saldo' es cuanta plata hay, "
                        "'movimientos' son las ultimas operaciones, 'todo' es "
                        "un resumen general."
                    ),
                },
                "fondo": {
                    "type": "string",
                    "description": "Fondo puntual por el que pregunta. Omitir si pregunta por todo.",
                },
            },
            "required": ["que"],
        },
    },
    {
        "name": "no_entendido",
        "description": (
            "Usar cuando el mensaje no describe ninguna operacion de plata o "
            "es demasiado ambiguo para actuar. Es preferible esto antes que "
            "adivinar un monto o un concepto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Que le falta al mensaje, en una frase, para pedirselo a la persona.",
                },
            },
            "required": ["motivo"],
        },
    },
]


#: Los nombres validos, para chequear rapido en los tests y en el router.
NOMBRES = frozenset(t["name"] for t in TOOLS)


# ---------------------------------------------------------------------------
# Tool aparte: matcheo de un pago de Mercado Pago con una reserva activa.
#
# No va en TOOLS porque no es algo que el usuario pueda pedir: la usa el job de
# Mercado Pago con su propia llamada a Claude.
# ---------------------------------------------------------------------------

TOOL_MATCH_RESERVA = {
    "name": "evaluar_match",
    "description": (
        "Decidir si un pago corresponde a alguna de las reservas activas. "
        "Por ejemplo un pago a 'GALENO SA' corresponde a una reserva "
        "'prepaga'. Si ninguna encaja con bastante claridad, devolver "
        "reserva_id nulo: es preferible no matchear que matchear mal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reserva_id": {
                "type": ["integer", "null"],
                "description": "Id de la reserva que corresponde, o null si ninguna.",
            },
            "motivo": {
                "type": "string",
                "description": "Por que si o por que no, en una frase corta.",
            },
            "categoria": {
                "type": "string",
                "description": "Categoria del gasto en una palabra (salud, comida, transporte...).",
            },
            "descripcion": {
                "type": "string",
                "description": "Descripcion corta y legible del pago, para mostrarle a la persona.",
            },
        },
        "required": ["reserva_id", "categoria", "descripcion"],
    },
}
