"""
Tests del formato de los mensajes. Sin base de datos.

Son textos, pero un error aca se ve feo todos los dias: plata mal formateada,
o un aviso que no aparece cuando un gasto se paso de la reserva.
"""

from decimal import Decimal

from tracker.chat import mensajes
from tracker.store.reglas import planificar_consumo_reserva


def test_la_plata_se_muestra_a_la_argentina():
    assert mensajes.plata(Decimal("1234567.89")) == "$1.234.567,89"
    assert mensajes.plata(Decimal("-12000")) == "-$12.000,00"
    assert mensajes.plata(0) == "$0,00"


def test_la_tna_se_muestra_sin_ceros_de_mas_ni_notacion_cientifica():
    assert mensajes.porcentaje(Decimal("0.35")) == "35"
    assert mensajes.porcentaje(Decimal("0.375")) == "37.5"
    # normalize() sola convertiria esto en "1E+2".
    assert mensajes.porcentaje(Decimal("1.00")) == "100"


def test_avisa_cuando_un_fondo_queda_en_negativo():
    texto = mensajes.gasto_registrado(5000, "farmacia", "sueldo", Decimal("-1000"))
    assert "negativo" in texto


def test_no_avisa_de_negativo_cuando_hay_saldo():
    texto = mensajes.gasto_registrado(5000, "farmacia", "sueldo", Decimal("1000"))
    assert "negativo" not in texto


def test_el_mensaje_de_reserva_avisa_del_sobrante():
    plan = planificar_consumo_reserva(420000, 400000)
    texto = mensajes.reserva_consumida(plan, "prepaga", "sueldo", Decimal("600000"))

    assert "Sobraron $20.000,00" in texto


def test_el_mensaje_de_reserva_avisa_del_excedente():
    # Este es el caso que el enunciado pide avisar por Telegram.
    plan = planificar_consumo_reserva(420000, 500000)
    texto = mensajes.reserva_consumida(plan, "prepaga", "sueldo", Decimal("500000"))

    assert "Se paso $80.000,00" in texto


def test_el_resumen_muestra_saldo_y_reservado_por_separado():
    texto = mensajes.resumen_general(
        [{"nombre": "sueldo", "saldo": Decimal("580000"), "comprometido": Decimal("420000")}],
        [{"concepto": "prepaga", "monto": Decimal("420000"), "fondo": "sueldo"}],
        [],
    )

    assert "$580.000,00" in texto      # disponible
    assert "$420.000,00 reservado" in texto
    assert "$1.000.000,00" in texto    # total


def test_las_listas_vacias_dicen_algo_util():
    assert "No tenes ninguna reserva" in mensajes.lista_reservas([])
    assert "No tenes plazos fijos" in mensajes.lista_inversiones([])
    assert "No hay movimientos" in mensajes.lista_movimientos([])
