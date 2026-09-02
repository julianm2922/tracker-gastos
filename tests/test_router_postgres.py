"""
Tests del router: de una operacion interpretada a asientos escritos.

Igual que test_store_postgres.py, corren solo si esta seteada TEST_DATABASE_URL
apuntando a una base descartable.

Aca se prueban los caminos completos que mas van a usarse: cargar un gasto,
corregirlo por reply, anularlo, y las preguntas del bot (desambiguacion y
match con una reserva).
"""

from decimal import Decimal

import pytest

from tracker.chat import router
from tracker.interpreter.claude import Operacion
from tracker.store import asientos, fondos, inversiones, pendientes, reservas


@pytest.fixture
def sueldo(conn):
    return fondos.obtener_o_crear(conn, "sueldo")


def op(nombre, **args):
    """Atajo para armar la operacion que habria devuelto el interprete."""
    return Operacion(nombre=nombre, args=args)


def ejecutar(conn, nombre, *, mensaje=None, referido=None, pendiente=None, **args):
    return router.ejecutar(
        conn,
        op(nombre, **args),
        telegram_message_id=mensaje,
        asiento_referido=referido,
        pendiente=pendiente,
    )


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

def test_un_gasto_sin_fondo_sale_del_sueldo(conn, sueldo):
    respuesta = ejecutar(conn, "registrar_gasto", monto=12000,
                         descripcion="farmacia", categoria="salud")

    asiento = asientos.obtener(conn, respuesta.asiento_id)
    assert asiento["fondo_id"] == sueldo["id"]
    assert asiento["monto"] == Decimal("-12000.00")
    assert "farmacia" in respuesta.texto


def test_un_gasto_de_otro_fondo_crea_el_fondo_si_no_existia(conn):
    ejecutar(conn, "registrar_gasto", monto=250000,
             descripcion="albañil", fondo="ahorro")

    ahorro = fondos.obtener_por_nombre(conn, "ahorro")
    assert ahorro is not None
    assert asientos.saldo(conn, ahorro["id"]) == Decimal("-250000.00")


def test_un_ingreso_suma(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1200000, descripcion="sueldo de marzo")
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("1200000.00")


def test_crear_una_reserva_la_saca_del_disponible(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    ejecutar(conn, "crear_reserva", monto=420000, concepto="prepaga")

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("580000.00")
    assert asientos.comprometido(conn, sueldo["id"]) == Decimal("420000.00")


def test_crear_un_plazo_fijo(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    respuesta = ejecutar(conn, "crear_inversion", capital=500000,
                         tna_porcentaje=35, plazo_dias=30)

    activas = inversiones.listar_activas(conn)
    assert len(activas) == 1
    # El porcentaje que dijo la persona (35) se guarda como fraccion.
    assert activas[0]["tna"] == Decimal("0.3500")
    assert "$14.383,56" in respuesta.texto  # interes estimado
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("500000.00")


# ---------------------------------------------------------------------------
# Gastar contra una reserva
# ---------------------------------------------------------------------------

def test_un_gasto_contra_una_reserva_la_consume(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    ejecutar(conn, "crear_reserva", monto=420000, concepto="prepaga")

    respuesta = ejecutar(conn, "registrar_gasto", monto=400000,
                         descripcion="Galeno", reserva="prepaga")

    assert "Sobraron" in respuesta.texto
    assert reservas.listar_activas(conn) == []
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("600000.00")


def test_si_no_existe_esa_reserva_se_carga_como_gasto_comun(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")

    ejecutar(conn, "registrar_gasto", monto=5000, descripcion="prepaga",
             reserva="prepaga")

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("995000.00")


def test_con_dos_reservas_parecidas_pregunta_en_vez_de_adivinar(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    ejecutar(conn, "crear_reserva", monto=100000, concepto="prepaga mia")
    ejecutar(conn, "crear_reserva", monto=200000, concepto="prepaga de mi vieja")

    respuesta = ejecutar(conn, "registrar_gasto", monto=90000,
                         descripcion="Galeno", reserva="prepaga")

    assert respuesta.pendiente_id is not None
    assert "1." in respuesta.texto and "2." in respuesta.texto
    # Hasta que no conteste, no se toco nada.
    assert len(reservas.listar_activas(conn)) == 2
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("700000.00")


def test_al_elegir_una_opcion_se_ejecuta_lo_que_habia_quedado_pendiente(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    ejecutar(conn, "crear_reserva", monto=100000, concepto="prepaga mia")
    ejecutar(conn, "crear_reserva", monto=200000, concepto="prepaga de mi vieja")
    respuesta = ejecutar(conn, "registrar_gasto", monto=90000,
                         descripcion="Galeno", reserva="prepaga")

    pendiente = pendientes.obtener(conn, respuesta.pendiente_id)
    ejecutar(conn, "responder_pendiente", confirma=True, opcion=1,
             pendiente=pendiente)

    activas = reservas.listar_activas(conn)
    assert [r["concepto"] for r in activas] == ["prepaga de mi vieja"]
    assert pendientes.obtener(conn, respuesta.pendiente_id)["estado"] == "resuelto"


# ---------------------------------------------------------------------------
# Correcciones por reply
# ---------------------------------------------------------------------------

def test_corregir_el_monto_respondiendo_al_mensaje(conn, sueldo):
    # "gaste 12000 en la farmacia" ... "che, eran 15000 no 12000"
    primera = ejecutar(conn, "registrar_gasto", monto=12000,
                       descripcion="farmacia", mensaje=1)
    original = asientos.obtener(conn, primera.asiento_id)

    ejecutar(conn, "modificar_operacion", monto_nuevo=15000,
             referido=original, mensaje=2)

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("-15000.00")
    assert asientos.esta_anulado(conn, original["id"])


def test_corregir_mantiene_la_descripcion_si_no_se_cambia(conn, sueldo):
    primera = ejecutar(conn, "registrar_gasto", monto=12000,
                       descripcion="farmacia", categoria="salud", mensaje=1)
    original = asientos.obtener(conn, primera.asiento_id)

    segunda = ejecutar(conn, "modificar_operacion", monto_nuevo=15000,
                       referido=original)
    nuevo = asientos.obtener(conn, segunda.asiento_id)

    assert nuevo["descripcion"] == "farmacia"
    assert nuevo["categoria"] == "salud"


def test_anular_respondiendo_al_mensaje(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    gasto = ejecutar(conn, "registrar_gasto", monto=12000, descripcion="farmacia")
    original = asientos.obtener(conn, gasto.asiento_id)

    ejecutar(conn, "anular_operacion", referido=original)

    assert asientos.saldo(conn, sueldo["id"]) == Decimal("1000000.00")


def test_anular_la_creacion_de_una_reserva_tambien_la_cierra(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    creada = ejecutar(conn, "crear_reserva", monto=420000, concepto="prepaga")

    ejecutar(conn, "anular_operacion", referido=asientos.obtener(conn, creada.asiento_id))

    # Ni figura como activa ni sigue descontando del disponible.
    assert reservas.listar_activas(conn) == []
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("1000000.00")


def test_anular_un_plazo_fijo_lo_saca_de_los_vencimientos(conn, sueldo):
    creada = ejecutar(conn, "crear_inversion", capital=500000,
                      tna_porcentaje=35, plazo_dias=30)

    ejecutar(conn, "anular_operacion", referido=asientos.obtener(conn, creada.asiento_id))

    assert inversiones.listar_activas(conn) == []
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("0.00")


def test_sin_reply_y_sin_referencia_pide_aclaracion(conn, sueldo):
    ejecutar(conn, "registrar_gasto", monto=12000, descripcion="farmacia")

    respuesta = ejecutar(conn, "anular_operacion")

    assert "no se a cual te referis" in respuesta.texto
    # Y no anulo nada.
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("-12000.00")


def test_sin_reply_con_varios_candidatos_pregunta_cual(conn, sueldo):
    ejecutar(conn, "registrar_gasto", monto=1000, descripcion="farmacia del centro")
    ejecutar(conn, "registrar_gasto", monto=2000, descripcion="farmacia de la esquina")

    respuesta = ejecutar(conn, "anular_operacion", referencia="farmacia")

    assert respuesta.pendiente_id is not None
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("-3000.00")  # intacto


def test_un_asiento_de_reserva_no_se_corrige_directo(conn, sueldo):
    creada = ejecutar(conn, "crear_reserva", monto=420000, concepto="prepaga")

    respuesta = ejecutar(conn, "modificar_operacion", monto_nuevo=500000,
                         referido=asientos.obtener(conn, creada.asiento_id))

    assert "Anulalo y volve a cargarlo" in respuesta.texto
    assert reservas.listar_activas(conn)[0]["monto"] == Decimal("420000.00")


# ---------------------------------------------------------------------------
# Pendientes que vienen de Mercado Pago
# ---------------------------------------------------------------------------

def test_confirmar_un_match_de_mercado_pago_consume_la_reserva(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    reserva, _ = reservas.crear(conn, fondo_id=sueldo["id"],
                                concepto="prepaga", monto=420000)
    pendiente = pendientes.crear(conn, tipo="match_reserva", payload={
        "reserva_id": reserva["id"],
        "monto": "400000",
        "descripcion": "GALENO SA",
        "categoria": "salud",
        "fecha": "2026-09-01",
        "origen_ref": "mp-123",
        "fondo": "sueldo",
    })

    router.resolver_pendiente(conn, pendiente, confirma=True)

    assert reservas.obtener(conn, reserva["id"])["estado"] == "consumida"
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("600000.00")
    assert asientos.existe_origen(conn, "mp-123")


def test_rechazar_el_match_igual_registra_el_pago(conn, sueldo):
    # El pago existio: que no sea de la reserva no significa que no haya salido.
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    reserva, _ = reservas.crear(conn, fondo_id=sueldo["id"],
                                concepto="prepaga", monto=420000)
    pendiente = pendientes.crear(conn, tipo="match_reserva", payload={
        "reserva_id": reserva["id"],
        "monto": "5000",
        "descripcion": "otra cosa",
        "categoria": "otros",
        "fecha": "2026-09-01",
        "origen_ref": "mp-456",
        "fondo": "sueldo",
    })

    router.resolver_pendiente(conn, pendiente, confirma=False)

    assert reservas.obtener(conn, reserva["id"])["estado"] == "activa"
    assert asientos.saldo(conn, sueldo["id"]) == Decimal("575000.00")
    assert asientos.existe_origen(conn, "mp-456")


# ---------------------------------------------------------------------------
# Consultas y errores
# ---------------------------------------------------------------------------

def test_consultar_el_resumen(conn, sueldo):
    ejecutar(conn, "registrar_ingreso", monto=1000000, descripcion="sueldo")
    ejecutar(conn, "crear_reserva", monto=420000, concepto="prepaga")

    respuesta = ejecutar(conn, "consultar", que="todo")

    assert "$580.000,00" in respuesta.texto
    assert "prepaga" in respuesta.texto


def test_no_entendido_no_escribe_nada(conn, sueldo):
    respuesta = ejecutar(conn, "no_entendido", motivo="no dijiste el monto")

    assert "no dijiste el monto" in respuesta.texto
    assert asientos.ultimos(conn) == []


def test_un_error_de_reglas_se_contesta_en_castellano(conn, sueldo):
    # Anular dos veces lo mismo: el store lanza OperacionInvalida y el router
    # lo convierte en un mensaje, en vez de dejar que reviente el job.
    gasto = ejecutar(conn, "registrar_gasto", monto=1000, descripcion="test")
    asiento = asientos.obtener(conn, gasto.asiento_id)
    ejecutar(conn, "anular_operacion", referido=asiento)

    respuesta = ejecutar(conn, "anular_operacion", referido=asiento)

    assert "No pude hacer eso" in respuesta.texto
