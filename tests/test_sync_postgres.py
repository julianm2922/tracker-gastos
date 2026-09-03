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


# ---------------------------------------------------------------------------
# La logica de la tarea de reporte (tracker/jobs/sync_mercadopago.py)
#
# MP no entrega el reporte al toque: hay que pedirlo, y despues consultar si
# ya termino. Como el job corre una vez por dia, el id de esa tarea se guarda
# en estado_app para retomarla en la proxima corrida en vez de perderla o
# pedir una nueva de mas.
# ---------------------------------------------------------------------------

from datetime import timedelta
from unittest.mock import patch

from tracker import fechas
from tracker.jobs import sync_mercadopago as job
from tracker.store import estado


class MPFalso:
    """Reemplaza a tracker.movements.mercadopago para los tests del job."""

    def __init__(self):
        self.pedidos = []
        self.consultas = []
        self.tarea_a_devolver = {"id": 111, "status": "pending"}
        self.csv_a_devolver = "SOURCE_ID,TRANSACTION_AMOUNT,TRANSACTION_DATE\n"

    def pedir_reporte(self, desde, hasta):
        self.pedidos.append((desde, hasta))
        return dict(self.tarea_a_devolver)

    def consultar_tarea(self, tarea_id):
        self.consultas.append(tarea_id)
        return dict(self.tarea_a_devolver)

    def descargar_reporte(self, nombre):
        return self.csv_a_devolver

    def normalizar_csv(self, texto):
        from tracker.movements.mercadopago import normalizar_csv
        return normalizar_csv(texto)


def test_si_no_hay_tarea_pendiente_pide_una_y_la_guarda(conn):
    mp = MPFalso()
    mp.tarea_a_devolver = {"id": 42, "status": "pending"}

    with patch.object(job, "mercadopago", mp):
        job.importar_movimientos(conn, chat_id=1)

    assert len(mp.pedidos) == 1
    assert estado.obtener(conn, job.CLAVE_TAREA_ID) == "42"
    assert estado.obtener(conn, job.CLAVE_TAREA_FECHA) == fechas.hoy().isoformat()


def test_si_la_tarea_no_termino_no_pide_una_nueva(conn):
    estado.guardar(conn, job.CLAVE_TAREA_ID, "42")
    estado.guardar(conn, job.CLAVE_TAREA_FECHA, fechas.hoy().isoformat())

    mp = MPFalso()
    mp.tarea_a_devolver = {"id": 42, "status": "pending"}

    with patch.object(job, "mercadopago", mp):
        job.importar_movimientos(conn, chat_id=1)

    assert mp.pedidos == []  # no pidio una nueva
    assert mp.consultas == ["42"]
    # sigue guardada para la proxima corrida
    assert estado.obtener(conn, job.CLAVE_TAREA_ID) == "42"


def test_cuando_la_tarea_termina_se_procesa_y_se_libera(conn, sueldo):
    estado.guardar(conn, job.CLAVE_TAREA_ID, "42")
    estado.guardar(conn, job.CLAVE_TAREA_FECHA, fechas.hoy().isoformat())

    mp = MPFalso()
    mp.tarea_a_devolver = {"id": 42, "status": "processed", "file_name": "r.csv"}
    mp.csv_a_devolver = (
        "SOURCE_ID,TRANSACTION_AMOUNT,TRANSACTION_DATE\n"
        "1,150000,2026-09-01\n"
    )

    with patch.object(job, "mercadopago", mp):
        job.importar_movimientos(conn, chat_id=1)

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("150000.00")
    # se libera: la proxima corrida puede pedir un reporte nuevo
    assert estado.obtener(conn, job.CLAVE_TAREA_ID) is None


def test_una_tarea_vieja_se_abandona_y_se_pide_otra(conn):
    vieja = fechas.hoy() - timedelta(days=job.MAX_DIAS_ESPERANDO_TAREA + 1)
    estado.guardar(conn, job.CLAVE_TAREA_ID, "42")
    estado.guardar(conn, job.CLAVE_TAREA_FECHA, vieja.isoformat())

    mp = MPFalso()
    mp.tarea_a_devolver = {"id": 99, "status": "pending"}

    with patch.object(job, "mercadopago", mp):
        job.importar_movimientos(conn, chat_id=1)

    assert len(mp.pedidos) == 1         # abandono la 42 y pidio una nueva
    assert "42" not in mp.consultas     # nunca pregunto por la abandonada
    assert mp.consultas == [99]         # solo consulto la nueva, recien pedida
    assert estado.obtener(conn, job.CLAVE_TAREA_ID) == "99"


def test_una_tarea_reciente_no_se_abandona(conn):
    reciente = fechas.hoy() - timedelta(days=1)
    estado.guardar(conn, job.CLAVE_TAREA_ID, "42")
    estado.guardar(conn, job.CLAVE_TAREA_FECHA, reciente.isoformat())

    mp = MPFalso()
    mp.tarea_a_devolver = {"id": 42, "status": "pending"}

    with patch.object(job, "mercadopago", mp):
        job.importar_movimientos(conn, chat_id=1)

    assert mp.pedidos == []
    assert mp.consultas == ["42"]
