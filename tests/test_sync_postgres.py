"""
Tests del flujo de Mercado Pago (necesitan TEST_DATABASE_URL).

Claude se reemplaza por una funcion falsa: lo que interesa probar no es que
matchee bien semanticamente, sino que el sistema haga lo correcto con cada
respuesta posible, y sobre todo que no toque saldos antes de preguntar.
"""

from datetime import date
from decimal import Decimal

import pytest

from tracker.movements import sync
from tracker.store import asientos, fondos, pendientes, reservas


class ClaudeFalso:
    """Devuelve siempre la misma evaluacion de match."""

    def __init__(self, reserva_id=None, categoria="otros", descripcion="un pago"):
        self.respuesta = {
            "reserva_id": reserva_id,
            "categoria": categoria,
            "descripcion": descripcion,
        }
        self.messages = self

    def create(self, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name="evaluar_match",
                                     input=self.respuesta)],
            stop_reason="tool_use",
        )


def movimiento(ref="mp-1", monto="-4500.50", descripcion="GALENO SA"):
    return {
        "origen_ref": ref,
        "monto": Decimal(monto),
        "fecha": date(2026, 9, 1),
        "descripcion": descripcion,
    }


@pytest.fixture
def sueldo(conn):
    return fondos.obtener_o_crear(conn, "sueldo")


def test_un_pago_que_no_matchea_se_registra_como_gasto(conn, sueldo):
    resultado = sync.procesar(
        conn, [movimiento()],
        cliente_claude=ClaudeFalso(reserva_id=None, categoria="salud",
                                   descripcion="Farmacia"),
    )

    assert resultado.registrados == 1
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("-4500.50")
    assert asientos.existe_origen(conn, "mp-1")


def test_un_ingreso_se_registra_sin_preguntar_nada(conn, sueldo):
    # Una reserva es plata apartada para gastar; un cobro no se matchea contra
    # ninguna, asi que ni se le pregunta a Claude.
    resultado = sync.procesar(conn, [movimiento(monto="150000")], cliente_claude=None)

    assert resultado.registrados == 1
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("150000.00")


def test_un_pago_que_matchea_una_reserva_solo_pregunta(conn, sueldo):
    reserva, _ = reservas.crear(conn, fondo_id=sueldo["id"],
                                concepto="prepaga", monto=420000)
    saldo_antes = asientos.saldo(conn, sueldo["id"])

    resultado = sync.procesar(
        conn, [movimiento(monto="-400000")],
        cliente_claude=ClaudeFalso(reserva_id=reserva["id"]),
    )

    assert resultado.registrados == 0
    assert len(resultado.preguntas) == 1
    # Lo importante: nada se movio.
    assert asientos.saldo(conn, sueldo["id"]) == saldo_antes
    assert reservas.obtener(conn, reserva["id"])["estado"] == "activa"


def test_no_se_importa_dos_veces_el_mismo_movimiento(conn, sueldo):
    sync.procesar(conn, [movimiento()], cliente_claude=ClaudeFalso())
    resultado = sync.procesar(conn, [movimiento()], cliente_claude=ClaudeFalso())

    assert resultado.registrados == 0
    assert resultado.duplicados == 1


def test_no_se_vuelve_a_preguntar_por_un_pago_con_pregunta_abierta(conn, sueldo):
    # Como el pendiente todavia no escribio ningun asiento, sin este chequeo la
    # corrida del dia siguiente preguntaria de nuevo por el mismo pago.
    reserva, _ = reservas.crear(conn, fondo_id=sueldo["id"],
                                concepto="prepaga", monto=420000)
    claude_falso = ClaudeFalso(reserva_id=reserva["id"])

    sync.procesar(conn, [movimiento(monto="-400000")], cliente_claude=claude_falso)
    resultado = sync.procesar(conn, [movimiento(monto="-400000")],
                              cliente_claude=claude_falso)

    assert resultado.preguntas == []
    assert resultado.duplicados == 1
    assert len(pendientes.listar_esperando(conn)) == 1


def test_un_id_de_reserva_inventado_no_llega_a_la_base(conn, sueldo):
    # Si el modelo devuelve un id que no existe, se ignora el match y el pago
    # se registra como un gasto comun.
    resultado = sync.procesar(
        conn, [movimiento()], cliente_claude=ClaudeFalso(reserva_id=9999)
    )

    assert resultado.registrados == 1
    assert resultado.preguntas == []
