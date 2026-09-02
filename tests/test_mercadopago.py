"""
Tests del parseo del reporte de Mercado Pago.

El CSV de MP no tiene un formato unico, asi que lo que se prueba aca es que el
parser sea tolerante: que encuentre los datos aunque cambien los nombres de las
columnas, y que descarte las filas que no sirven en vez de romper.
"""

from datetime import date
from decimal import Decimal

from tracker.movements.mercadopago import normalizar_csv


def test_parsea_un_reporte_tipico():
    reporte = (
        "SOURCE_ID,TRANSACTION_AMOUNT,TRANSACTION_DATE,DESCRIPTION\n"
        "12345678,-4500.50,2026-09-01T10:30:00Z,GALENO SA\n"
        "12345679,150000.00,2026-09-02T09:00:00Z,Transferencia recibida\n"
    )

    movimientos = normalizar_csv(reporte)

    assert len(movimientos) == 2
    assert movimientos[0] == {
        "origen_ref": "mp-12345678",
        "monto": Decimal("-4500.50"),
        "fecha": date(2026, 9, 1),
        "descripcion": "GALENO SA",
    }
    # El signo se respeta: positivo es plata que entro.
    assert movimientos[1]["monto"] > 0


def test_el_origen_ref_lleva_prefijo_para_no_chocar_con_otras_fuentes():
    movimientos = normalizar_csv(
        "SOURCE_ID,AMOUNT,DATE_CREATED\n99,-100,2026-09-01\n"
    )
    assert movimientos[0]["origen_ref"] == "mp-99"


def test_acepta_nombres_de_columna_alternativos():
    reporte = (
        "OPERATION_ID,TRANSACTION_NET_AMOUNT,MONEY_RELEASE_DATE,PAYER_NAME\n"
        "555,-999.99,01/09/2026,Farmacity\n"
    )

    movimientos = normalizar_csv(reporte)

    assert movimientos[0]["origen_ref"] == "mp-555"
    assert movimientos[0]["monto"] == Decimal("-999.99")
    assert movimientos[0]["fecha"] == date(2026, 9, 1)
    assert movimientos[0]["descripcion"] == "Farmacity"


def test_descarta_filas_incompletas_sin_romper():
    reporte = (
        "SOURCE_ID,TRANSACTION_AMOUNT,TRANSACTION_DATE,DESCRIPTION\n"
        ",-100,2026-09-01,sin id\n"
        "1,,2026-09-01,sin monto\n"
        "2,-100,fecha rara,fecha ilegible\n"
        "3,0,2026-09-01,monto cero\n"
        "4,-100,2026-09-01,este si sirve\n"
    )

    movimientos = normalizar_csv(reporte)

    assert [m["origen_ref"] for m in movimientos] == ["mp-4"]


def test_un_reporte_vacio_no_rompe():
    assert normalizar_csv("SOURCE_ID,TRANSACTION_AMOUNT,TRANSACTION_DATE\n") == []
    assert normalizar_csv("") == []
