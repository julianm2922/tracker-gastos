"""
Tests del interprete. No llaman a la API de verdad: se le pasa un cliente
falso, se mira que se le pidio a Claude y se comprueba que la respuesta se
traduzca bien a una Operacion.

Lo que se verifica es el contrato: que siempre se lo obligue a usar una tool
(nunca prosa libre), que el contexto llegue en el system prompt, y que una
foto viaje como imagen.
"""

import base64
from datetime import date
from types import SimpleNamespace

import pytest

from tracker.interpreter import claude
from tracker.interpreter.tools import NOMBRES, TOOLS


class ClienteFalso:
    """Se hace pasar por anthropic.Anthropic y guarda con que lo llamaron."""

    def __init__(self, nombre_tool="registrar_gasto", args=None, texto_libre=False):
        self.nombre_tool = nombre_tool
        self.args = args or {"monto": 12000, "descripcion": "farmacia"}
        self.texto_libre = texto_libre
        self.llamada = None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.llamada = kwargs
        if self.texto_libre:
            bloques = [SimpleNamespace(type="text", text="hola!")]
        else:
            bloques = [
                SimpleNamespace(
                    type="tool_use", name=self.nombre_tool, input=self.args
                )
            ]
        return SimpleNamespace(content=bloques, stop_reason="tool_use")


@pytest.fixture
def contexto():
    return claude.Contexto(hoy=date(2026, 9, 2), fondos=["sueldo", "ahorro"])


# ---------------------------------------------------------------------------
# Las definiciones de las tools
# ---------------------------------------------------------------------------

def test_cada_tool_tiene_nombre_descripcion_y_schema():
    for tool in TOOLS:
        assert tool["name"]
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"


def test_los_campos_obligatorios_estan_definidos_en_el_schema():
    # Pedir como required un campo que no existe en properties es un error que
    # solo se ve cuando la API contesta 400 en produccion.
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert set(schema.get("required", [])) <= set(schema["properties"]), tool["name"]


def test_hay_una_salida_para_cuando_no_se_entiende():
    # Como forzamos tool_choice="any", Claude tiene que llamar a algo siempre.
    # Sin esta tool, ante un mensaje ambiguo inventaria un gasto.
    assert "no_entendido" in NOMBRES


# ---------------------------------------------------------------------------
# La llamada
# ---------------------------------------------------------------------------

def test_se_obliga_a_claude_a_usar_una_sola_tool(contexto):
    cliente = ClienteFalso()
    claude.interpretar("gaste 12000 en la farmacia", contexto=contexto, cliente=cliente)

    assert cliente.llamada["tool_choice"]["type"] == "any"
    assert cliente.llamada["tool_choice"]["disable_parallel_tool_use"] is True
    assert cliente.llamada["tools"] == TOOLS


def test_la_respuesta_se_traduce_a_una_operacion(contexto):
    cliente = ClienteFalso(args={"monto": 12000, "descripcion": "farmacia"})

    operacion = claude.interpretar("gaste 12000", contexto=contexto, cliente=cliente)

    assert operacion.nombre == "registrar_gasto"
    assert operacion.args["monto"] == 12000
    assert operacion.arg("fondo") is None  # no vino: el store usara "sueldo"


def test_un_string_vacio_cuenta_como_dato_ausente(contexto):
    # Claude a veces manda "" en vez de omitir el campo.
    cliente = ClienteFalso(args={"monto": 1, "descripcion": "x", "fondo": ""})
    operacion = claude.interpretar("x", contexto=contexto, cliente=cliente)

    assert operacion.arg("fondo", "sueldo") == "sueldo"


def test_si_claude_contesta_prosa_es_un_error(contexto):
    cliente = ClienteFalso(texto_libre=True)

    with pytest.raises(claude.InterpretacionFallida):
        claude.interpretar("hola", contexto=contexto, cliente=cliente)


def test_no_se_puede_interpretar_un_mensaje_vacio(contexto):
    with pytest.raises(claude.InterpretacionFallida):
        claude.interpretar(None, contexto=contexto, cliente=ClienteFalso())


def test_una_foto_viaja_como_imagen(contexto):
    cliente = ClienteFalso()
    claude.interpretar(None, contexto=contexto, imagen=b"\x89PNG-falso",
                       media_type="image/png", cliente=cliente)

    contenido = cliente.llamada["messages"][0]["content"]
    imagen = contenido[0]
    assert imagen["type"] == "image"
    assert imagen["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(imagen["source"]["data"]) == b"\x89PNG-falso"
    # Y ademas va una consigna de texto, para que sepa que hacer con la foto.
    assert contenido[1]["type"] == "text"


def test_el_modelo_es_haiku(contexto):
    cliente = ClienteFalso()
    claude.interpretar("hola", contexto=contexto, cliente=cliente)
    assert cliente.llamada["model"] == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# El system prompt
# ---------------------------------------------------------------------------

def test_el_prompt_lleva_la_fecha_y_los_fondos(contexto):
    prompt = claude.armar_system_prompt(contexto)
    assert "2026-09-02" in prompt
    assert "sueldo, ahorro" in prompt


def test_el_prompt_lleva_la_operacion_a_la_que_se_responde():
    contexto = claude.Contexto(
        hoy=date(2026, 9, 2),
        asiento_referido={
            "id": 7, "tipo": "gasto", "monto": -12000,
            "descripcion": "farmacia", "fecha": date(2026, 9, 1),
        },
    )
    prompt = claude.armar_system_prompt(contexto)

    assert "id 7" in prompt
    assert "farmacia" in prompt


def test_el_prompt_avisa_cuando_hay_una_pregunta_abierta():
    contexto = claude.Contexto(
        hoy=date(2026, 9, 2),
        pendiente={"tipo": "match_reserva", "payload": {"reserva_id": 3}},
    )
    prompt = claude.armar_system_prompt(contexto)

    assert "responder_pendiente" in prompt


# ---------------------------------------------------------------------------
# Matcheo de pagos de Mercado Pago con reservas
# ---------------------------------------------------------------------------

def test_el_matcheo_de_reserva_fuerza_su_tool():
    cliente = ClienteFalso(
        nombre_tool="evaluar_match",
        args={"reserva_id": 3, "categoria": "salud", "descripcion": "Prepaga Galeno"},
    )

    resultado = claude.matchear_reserva(
        "GALENO SA", 420000, [{"id": 3, "concepto": "prepaga", "monto": 420000}],
        cliente=cliente,
    )

    assert cliente.llamada["tool_choice"] == {"type": "tool", "name": "evaluar_match"}
    assert resultado["reserva_id"] == 3
