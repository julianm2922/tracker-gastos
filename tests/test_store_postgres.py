"""
Tests del store contra un Postgres de verdad.

Corren solo si esta seteada TEST_DATABASE_URL, apuntando a una base
DESCARTABLE (el fixture `conn` borra todas las tablas antes de cada test).

    TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest tests/test_store_postgres.py

Lo que se prueba aca es que el SQL haga lo que las reglas dicen: sobre todo que
los saldos den bien despues de cada operacion.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tracker.store import asientos, fondos, inversiones, pendientes, reservas
from tracker.store.asientos import OperacionInvalida
from tracker.store.reglas import porcentaje_a_tna


@pytest.fixture
def sueldo(conn):
    """El fondo por defecto, que schema.sql ya deja creado."""
    return fondos.obtener_o_crear(conn, "sueldo")


# ---------------------------------------------------------------------------
# Fondos y saldos
# ---------------------------------------------------------------------------

def test_el_fondo_por_defecto_existe_despues_de_migrar(conn):
    assert fondos.obtener_por_nombre(conn, "sueldo") is not None


def test_se_puede_agregar_un_fondo_en_cualquier_momento(conn):
    bono = fondos.obtener_o_crear(conn, "  Bono ")
    assert bono["nombre"] == "bono"  # normalizado
    # Pedirlo de nuevo devuelve el mismo, no crea otro.
    assert fondos.obtener_o_crear(conn, "bono")["id"] == bono["id"]


def test_saldo_de_un_fondo_vacio_es_cero(conn, sueldo):
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("0.00")


def test_ingresos_y_gastos_se_suman_con_signo(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=1000000, tipo="ingreso")
    asientos.crear(conn, fondo_id=sueldo["id"], monto=-12000, tipo="gasto",
                   descripcion="farmacia", categoria="salud")

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("988000.00")


def test_un_fondo_puede_quedar_en_negativo(conn, sueldo):
    # Esta permitido: el sistema solo avisa, no lo impide.
    asientos.crear(conn, fondo_id=sueldo["id"], monto=-5000, tipo="gasto")
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("-5000.00")


def test_no_se_permite_un_asiento_en_cero(conn, sueldo):
    with pytest.raises(OperacionInvalida):
        asientos.crear(conn, fondo_id=sueldo["id"], monto=0, tipo="gasto")


# ---------------------------------------------------------------------------
# Reservas: los tres casos del enunciado
# ---------------------------------------------------------------------------

def test_crear_reserva_saca_del_saldo_pero_no_del_total(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=1000000, tipo="ingreso")
    reservas.crear(conn, fondo_id=sueldo["id"], concepto="prepaga", monto=420000)

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("580000.00")
    assert asientos.comprometido(conn, sueldo["id"]) == Decimal("420000.00")
    assert asientos.total(conn, sueldo["id"]) == Decimal("1000000.00")


def test_reserva_cancelada_devuelve_todo(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=1000000, tipo="ingreso")
    reserva, _ = reservas.crear(
        conn, fondo_id=sueldo["id"], concepto="prepaga", monto=420000
    )

    reserva_cerrada, _ = reservas.cancelar(conn, reserva["id"])

    assert reserva_cerrada["estado"] == "cancelada"
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("1000000.00")
    assert asientos.comprometido(conn, sueldo["id"]) == Decimal("0.00")


def test_no_se_puede_cancelar_dos_veces(conn, sueldo):
    reserva, _ = reservas.crear(
        conn, fondo_id=sueldo["id"], concepto="prepaga", monto=1000
    )
    reservas.cancelar(conn, reserva["id"])

    with pytest.raises(OperacionInvalida):
        reservas.cancelar(conn, reserva["id"])


def test_gasto_menor_a_la_reserva(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=1000000, tipo="ingreso")
    reserva, _ = reservas.crear(
        conn, fondo_id=sueldo["id"], concepto="prepaga", monto=420000
    )

    resultado = reservas.consumir(conn, reserva["id"], 400000,
                                  descripcion="Galeno", categoria="salud")

    assert resultado["reserva"]["estado"] == "consumida"
    assert resultado["plan"].sobrante == Decimal("20000.00")
    assert not resultado["plan"].hay_excedente
    # 1.000.000 - 400.000 realmente gastados.
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("600000.00")
    assert asientos.comprometido(conn, sueldo["id"]) == Decimal("0.00")


def test_gasto_mayor_a_la_reserva(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=1000000, tipo="ingreso")
    reserva, _ = reservas.crear(
        conn, fondo_id=sueldo["id"], concepto="prepaga", monto=420000
    )

    resultado = reservas.consumir(conn, reserva["id"], 500000)

    assert resultado["plan"].excedente == Decimal("80000.00")
    assert resultado["plan"].hay_excedente  # el bot tiene que avisar
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("500000.00")


def test_el_gasto_contra_reserva_queda_registrado_como_gasto(conn, sueldo):
    # Importa para poder mirar gastos por categoria despues.
    reserva, _ = reservas.crear(
        conn, fondo_id=sueldo["id"], concepto="prepaga", monto=1000
    )
    resultado = reservas.consumir(conn, reserva["id"], 900, categoria="salud")

    gasto = resultado["asiento_gasto"]
    assert gasto["tipo"] == "gasto"
    assert gasto["categoria"] == "salud"
    assert gasto["monto"] == Decimal("-900.00")


def test_no_se_puede_consumir_una_reserva_ya_cerrada(conn, sueldo):
    reserva, _ = reservas.crear(
        conn, fondo_id=sueldo["id"], concepto="prepaga", monto=1000
    )
    reservas.consumir(conn, reserva["id"], 500)

    with pytest.raises(OperacionInvalida):
        reservas.consumir(conn, reserva["id"], 100)


# ---------------------------------------------------------------------------
# Inversiones
# ---------------------------------------------------------------------------

def test_crear_plazo_fijo_saca_el_capital_del_fondo(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=1000000, tipo="ingreso")
    inversion, _ = inversiones.crear(
        conn, fondo_id=sueldo["id"], capital=500000,
        tna=porcentaje_a_tna(35), plazo_dias=30, fecha_inicio=date(2026, 1, 1),
    )

    assert inversion["fecha_vencimiento"] == date(2026, 1, 31)
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("500000.00")


def test_acreditar_devuelve_capital_mas_interes_al_fondo_de_origen(conn):
    ahorro = fondos.obtener_o_crear(conn, "ahorro")
    asientos.crear(conn, fondo_id=ahorro["id"], monto=1000000, tipo="ingreso")
    inversion, _ = inversiones.crear(
        conn, fondo_id=ahorro["id"], capital=500000,
        tna=porcentaje_a_tna(35), plazo_dias=30, fecha_inicio=date(2026, 1, 1),
    )

    resultado = inversiones.acreditar(conn, inversion["id"])

    assert resultado["interes"] == Decimal("14383.56")
    assert resultado["inversion"]["estado"] == "acreditada"
    # Vuelve al mismo fondo del que salio, no al de por defecto.
    assert asientos.saldo(conn, ahorro["id"]) == Decimal("1014383.56")
    assert asientos.saldo(conn, fondos.obtener_o_crear(conn)["id"]) == Decimal("0.00")


def test_un_plazo_fijo_no_se_acredita_dos_veces(conn, sueldo):
    inversion, _ = inversiones.crear(
        conn, fondo_id=sueldo["id"], capital=1000,
        tna=porcentaje_a_tna(35), plazo_dias=30, fecha_inicio=date(2026, 1, 1),
    )
    inversiones.acreditar(conn, inversion["id"])

    with pytest.raises(OperacionInvalida):
        inversiones.acreditar(conn, inversion["id"])


def test_listar_vencidas_solo_trae_las_que_ya_vencieron(conn, sueldo):
    hoy = date(2026, 3, 1)
    vencida, _ = inversiones.crear(
        conn, fondo_id=sueldo["id"], capital=1000, tna=Decimal("0.35"),
        plazo_dias=30, fecha_inicio=hoy - timedelta(days=60),
    )
    inversiones.crear(
        conn, fondo_id=sueldo["id"], capital=1000, tna=Decimal("0.35"),
        plazo_dias=30, fecha_inicio=hoy,
    )

    pendientes_de_acreditar = inversiones.listar_vencidas(conn, hoy)

    assert [i["id"] for i in pendientes_de_acreditar] == [vencida["id"]]


# ---------------------------------------------------------------------------
# Anulaciones
# ---------------------------------------------------------------------------

def test_anular_un_gasto_lo_deja_como_si_no_hubiera_pasado(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=1000000, tipo="ingreso")
    gasto = asientos.crear(conn, fondo_id=sueldo["id"], monto=-12000,
                           tipo="gasto", descripcion="farmacia")

    asientos.revertir(conn, gasto["id"])

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("1000000.00")
    assert asientos.esta_anulado(conn, gasto["id"])
    # El asiento original sigue estando: se anula, no se borra.
    assert asientos.obtener(conn, gasto["id"]) is not None


def test_no_se_puede_anular_dos_veces_el_mismo_asiento(conn, sueldo):
    gasto = asientos.crear(conn, fondo_id=sueldo["id"], monto=-1000, tipo="gasto")
    asientos.revertir(conn, gasto["id"])

    with pytest.raises(OperacionInvalida):
        asientos.revertir(conn, gasto["id"])


def test_modificar_es_anular_y_volver_a_crear(conn, sueldo):
    # "che, eran 15000 no 12000"
    original = asientos.crear(conn, fondo_id=sueldo["id"], monto=-12000,
                              tipo="gasto", descripcion="farmacia")

    asientos.revertir(conn, original["id"])
    asientos.crear(conn, fondo_id=sueldo["id"], monto=-15000,
                   tipo="gasto", descripcion="farmacia")

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("-15000.00")


def test_los_asientos_anulados_no_aparecen_como_candidatos(conn, sueldo):
    gasto = asientos.crear(conn, fondo_id=sueldo["id"], monto=-1000,
                           tipo="gasto", descripcion="farmacia del centro")
    assert len(asientos.buscar_por_descripcion(conn, "farmacia")) == 1

    asientos.revertir(conn, gasto["id"])
    assert asientos.buscar_por_descripcion(conn, "farmacia") == []


# ---------------------------------------------------------------------------
# Correcciones por reply y dedupe
# ---------------------------------------------------------------------------

def test_se_encuentra_el_asiento_por_el_mensaje_del_usuario_o_del_bot(conn, sueldo):
    gasto = asientos.crear(conn, fondo_id=sueldo["id"], monto=-1000, tipo="gasto",
                           telegram_message_id=111)
    asientos.marcar_mensaje_del_bot(conn, gasto["id"], 222)

    assert asientos.buscar_por_mensaje_telegram(conn, 111)["id"] == gasto["id"]
    assert asientos.buscar_por_mensaje_telegram(conn, 222)["id"] == gasto["id"]
    assert asientos.buscar_por_mensaje_telegram(conn, 999) is None


def test_no_se_importa_dos_veces_el_mismo_movimiento_de_mercado_pago(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=-1000, tipo="gasto",
                   canal="mercadopago", origen_ref="mp-123")

    assert asientos.existe_origen(conn, "mp-123")
    assert not asientos.existe_origen(conn, "mp-999")


# ---------------------------------------------------------------------------
# Pendientes
# ---------------------------------------------------------------------------

def test_un_pendiente_no_toca_ningun_saldo(conn, sueldo):
    asientos.crear(conn, fondo_id=sueldo["id"], monto=1000000, tipo="ingreso")

    pendiente = pendientes.crear(
        conn, tipo="match_reserva",
        payload={"monto": "420000", "reserva_id": 1},
    )

    assert pendiente["estado"] == "esperando"
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("1000000.00")


def test_el_pendiente_se_encuentra_por_el_mensaje_del_bot(conn):
    pendiente = pendientes.crear(conn, tipo="match_reserva", payload={},
                                 telegram_bot_message_id=555)

    assert pendientes.buscar_por_mensaje_del_bot(conn, 555)["id"] == pendiente["id"]

    pendientes.cerrar(conn, pendiente["id"], "resuelto")
    # Ya resuelto: no vuelve a aparecer como esperando.
    assert pendientes.buscar_por_mensaje_del_bot(conn, 555) is None
