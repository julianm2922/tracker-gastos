"""
Lectura de los movimientos de Mercado Pago.

MP no tiene un endpoint que te devuelva "los movimientos" directo: hay que
pedir un reporte, esperar a que lo genere y despues bajar el CSV. Son tres
pasos y por eso este flujo corre en su propio cron, mas espaciado (una vez por
dia alcanza).

Aviso honesto: los nombres de las columnas del reporte de MP cambian segun el
tipo de cuenta y la version del reporte. Por eso `normalizar_csv` busca cada
dato entre varios nombres posibles y saltea las filas que no entiende, en vez
de asumir un formato exacto. Si algun movimiento no aparece, el primer lugar
para mirar es COLUMNAS_* aca abajo.
"""

import csv
import io
from datetime import date, datetime
from decimal import InvalidOperation

import requests

from tracker import config
from tracker.store.reglas import a_decimal

BASE = "https://api.mercadopago.com"

#: Reporte "todas las transacciones" (conciliacion de cuenta).
RECURSO = "/v1/account/settlement_report"

TIMEOUT = 60

# Nombres posibles de cada columna en el CSV, en orden de preferencia. Son los
# nombres reales del reporte "todas las transacciones" segun la documentacion
# de Mercado Pago (developers > Reports > Account money > Campos del reporte).
COLUMNAS_ID = ("SOURCE_ID", "EXTERNAL_REFERENCE")
# SETTLEMENT_NET_AMOUNT es el monto neto que impacto el balance de la cuenta
# (ya con comisiones descontadas): es el numero que corresponde a un asiento
# nuestro. TRANSACTION_AMOUNT es el monto bruto, y queda como respaldo por si
# algun reporte no trae la columna neta.
COLUMNAS_MONTO = ("SETTLEMENT_NET_AMOUNT", "REAL_AMOUNT", "TRANSACTION_AMOUNT")
COLUMNAS_FECHA = ("TRANSACTION_DATE", "SETTLEMENT_DATE", "MONEY_RELEASE_DATE")
COLUMNAS_DESCRIPCION = ("DESCRIPTION", "PAYER_NAME", "TRANSACTION_TYPE", "PAYMENT_METHOD_TYPE")


class ErrorDeMercadoPago(Exception):
    """MP contesto algo que no esperabamos."""


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.mercadopago_access_token()}"}


def pedir_reporte(desde: date, hasta: date) -> dict:
    """
    Le pide a MP que genere el reporte de un rango de fechas.

    No devuelve el reporte: encola una "tarea" de generacion y devuelve su
    descripcion, algo como {"id": 2222, "status": "pending", ...}. Tarda desde
    unos segundos hasta varios minutos en terminar. Guardar el "id" es lo que
    permite consultar despues si ya esta lista, con `consultar_tarea`.
    """
    respuesta = requests.post(
        f"{BASE}{RECURSO}",
        headers=_headers(),
        json={
            "begin_date": f"{desde.isoformat()}T00:00:00Z",
            "end_date": f"{hasta.isoformat()}T23:59:59Z",
        },
        timeout=TIMEOUT,
    )
    if respuesta.status_code >= 400:
        raise ErrorDeMercadoPago(
            f"No pude pedir el reporte ({respuesta.status_code}): {respuesta.text[:300]}"
        )
    return respuesta.json()


def consultar_tarea(tarea_id) -> dict:
    """
    Consulta si una tarea de generacion de reporte (la que devolvio
    pedir_reporte) ya termino.

    Cuando el campo "status" del resultado es "processed", el campo
    "file_name" tiene el nombre del reporte listo para bajar con
    descargar_reporte(). Cualquier otro status quiere decir "todavia no".
    """
    respuesta = requests.get(
        f"{BASE}{RECURSO}/task/{tarea_id}", headers=_headers(), timeout=TIMEOUT
    )
    if respuesta.status_code >= 400:
        raise ErrorDeMercadoPago(
            f"No pude consultar la tarea {tarea_id} "
            f"({respuesta.status_code}): {respuesta.text[:300]}"
        )
    return respuesta.json()


def listar_reportes() -> list[dict]:
    """Reportes ya generados que estan para bajar, del mas nuevo al mas viejo."""
    respuesta = requests.get(
        f"{BASE}{RECURSO}/list", headers=_headers(), timeout=TIMEOUT
    )
    if respuesta.status_code >= 400:
        raise ErrorDeMercadoPago(
            f"No pude listar los reportes ({respuesta.status_code}): {respuesta.text[:300]}"
        )
    reportes = respuesta.json()
    return sorted(reportes, key=lambda r: r.get("created_from", ""), reverse=True)


def descargar_reporte(nombre_archivo: str) -> str:
    """Baja un reporte ya generado y devuelve el CSV como texto."""
    respuesta = requests.get(
        f"{BASE}{RECURSO}/{nombre_archivo}", headers=_headers(), timeout=TIMEOUT
    )
    if respuesta.status_code >= 400:
        raise ErrorDeMercadoPago(
            f"No pude bajar {nombre_archivo} ({respuesta.status_code})"
        )
    return respuesta.text


def _primera_columna(fila: dict, candidatas: tuple[str, ...]) -> str | None:
    """Devuelve el valor de la primera columna de `candidatas` que tenga algo."""
    for nombre in candidatas:
        valor = fila.get(nombre)
        if valor not in (None, ""):
            return valor
    return None


def _parsear_fecha(texto: str) -> date | None:
    """
    MP escribe las fechas de varias formas segun el reporte.

    Probamos los formatos que aparecen en la practica y, si ninguno da,
    devolvemos None (quien llama descarta la fila).
    """
    texto = texto.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(texto).date()
    except ValueError:
        pass
    for formato in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    return None


def normalizar_csv(texto_csv: str) -> list[dict]:
    """
    Convierte el CSV de MP en movimientos con la forma que usa el resto del
    sistema:

        {"origen_ref": str, "monto": Decimal (con signo), "fecha": date,
         "descripcion": str}

    Monto negativo = plata que salio. Las filas sin id, sin monto o sin fecha
    se descartan: sin esas tres cosas no se puede ni deduplicar ni registrar.
    """
    movimientos = []

    for fila in csv.DictReader(io.StringIO(texto_csv)):
        # Normalizamos los nombres de columna a MAYUSCULAS y sin espacios,
        # porque MP no es del todo consistente entre reportes.
        fila = {(k or "").strip().upper(): v for k, v in fila.items()}

        identificador = _primera_columna(fila, COLUMNAS_ID)
        monto_texto = _primera_columna(fila, COLUMNAS_MONTO)
        fecha_texto = _primera_columna(fila, COLUMNAS_FECHA)
        if not identificador or monto_texto is None or not fecha_texto:
            continue

        try:
            monto = a_decimal(str(monto_texto).replace(",", "."))
        except (InvalidOperation, ValueError):
            continue

        fecha = _parsear_fecha(str(fecha_texto))
        if fecha is None or monto == 0:
            continue

        descripcion = _primera_columna(fila, COLUMNAS_DESCRIPCION) or "movimiento de Mercado Pago"

        movimientos.append({
            "origen_ref": f"mp-{identificador}",
            "monto": monto,
            "fecha": fecha,
            "descripcion": str(descripcion).strip(),
        })

    return movimientos


