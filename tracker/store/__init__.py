"""
store: la base de datos y las reglas del libro de asientos.

Los submodulos se importan por nombre para que en el codigo se lea de donde
sale cada cosa:

    from tracker.store import asientos, fondos, reservas

    with db.conectar() as conn:
        fondo = fondos.obtener_o_crear(conn, "sueldo")
        asientos.crear(conn, fondo_id=fondo["id"], monto=-12000, tipo="gasto")
        print(asientos.saldo(conn, fondo["id"]))
"""

from tracker.store import (  # noqa: F401  (se re-exportan a proposito)
    asientos,
    db,
    estado,
    fondos,
    inversiones,
    pendientes,
    reglas,
    reservas,
)
from tracker.store.asientos import OperacionInvalida  # noqa: F401
