"""
Tests de las reglas de negocio puras. No necesitan base de datos.

Aca esta lo mas facil de romper sin darse cuenta: el calculo de interes y los
tres casos de una reserva.
"""

from datetime import date
from decimal import Decimal

import pytest

from tracker.store.reglas import (
    calcular_interes,
    calcular_vencimiento,
    planificar_consumo_reserva,
    porcentaje_a_tna,
    redondear,
    sumar_montos,
    tna_a_porcentaje,
)


# ---------------------------------------------------------------------------
# Plata en general
# ---------------------------------------------------------------------------

def test_los_montos_se_manejan_sin_errores_de_float():
    # Con float, 0.1 + 0.2 da 0.30000000000000004. Con Decimal no.
    assert sumar_montos(["0.1", "0.2"]) == Decimal("0.30")


def test_redondeo_a_centavos_hacia_arriba_en_el_medio():
    assert redondear("10.005") == Decimal("10.01")
    assert redondear("10.004") == Decimal("10.00")


def test_el_saldo_es_la_suma_con_signo():
    # Ingreso de 100.000, gasto de 12.000, reserva de 20.000 apartada.
    assert sumar_montos([100000, -12000, -20000]) == Decimal("68000.00")


# ---------------------------------------------------------------------------
# TNA
# ---------------------------------------------------------------------------

def test_el_porcentaje_se_guarda_como_fraccion():
    assert porcentaje_a_tna(35) == Decimal("0.35")
    assert tna_a_porcentaje(Decimal("0.35")) == Decimal("35.00")


def test_interes_del_ejemplo_del_enunciado():
    # 500.000 al 35% anual a 30 dias.
    # 500000 * (0.35 / 365) * 30 = 14383.5616...
    interes = calcular_interes(500000, porcentaje_a_tna(35), 30)
    assert interes == Decimal("14383.56")


def test_interes_a_365_dias_es_la_tna_completa():
    # A un año exacto, el interes tiene que ser el capital por la TNA.
    assert calcular_interes(1000, Decimal("0.35"), 365) == Decimal("350.00")


def test_interes_con_plazo_invalido():
    with pytest.raises(ValueError):
        calcular_interes(1000, Decimal("0.35"), 0)


def test_vencimiento_es_inicio_mas_plazo():
    assert calcular_vencimiento(date(2026, 1, 15), 30) == date(2026, 2, 14)


# ---------------------------------------------------------------------------
# Los tres casos de una reserva
# ---------------------------------------------------------------------------

def test_gasto_menor_a_la_reserva_devuelve_el_resto():
    # Reserva de 420.000 para la prepaga, termina saliendo 400.000.
    plan = planificar_consumo_reserva(420000, 400000)

    assert plan.sobrante == Decimal("20000.00")
    assert plan.excedente == Decimal("0.00")
    assert not plan.hay_excedente
    # La reserva ya habia sacado 420.000 del saldo; el neto tiene que quedar
    # en -400.000, o sea que el saldo sube 20.000 respecto de antes de gastar.
    assert plan.efecto_en_saldo == Decimal("20000.00")


def test_gasto_mayor_a_la_reserva_saca_la_diferencia_del_fondo_y_avisa():
    # Reserva de 420.000, la prepaga vino 500.000.
    plan = planificar_consumo_reserva(420000, 500000)

    assert plan.sobrante == Decimal("0.00")
    assert plan.excedente == Decimal("80000.00")
    assert plan.hay_excedente  # esto es lo que dispara el aviso por Telegram
    assert plan.efecto_en_saldo == Decimal("-80000.00")


def test_gasto_igual_a_la_reserva_no_deja_nada_colgado():
    plan = planificar_consumo_reserva(420000, 420000)

    assert plan.sobrante == Decimal("0.00")
    assert plan.excedente == Decimal("0.00")
    assert not plan.hay_excedente
    assert plan.efecto_en_saldo == Decimal("0.00")


def test_no_se_puede_consumir_una_reserva_con_un_gasto_negativo():
    with pytest.raises(ValueError):
        planificar_consumo_reserva(420000, -1)


def test_el_plan_no_pierde_centavos():
    plan = planificar_consumo_reserva("100.55", "33.33")
    assert plan.sobrante == Decimal("67.22")
    # Devolucion + gasto tienen que dar exactamente el efecto en saldo.
    assert plan.monto_reserva - plan.monto_gasto == plan.efecto_en_saldo
