"""
Reglas de negocio puras: plata, intereses y que asientos genera cada operacion.

Este modulo NO habla con la base de datos a proposito. Recibe numeros y
devuelve numeros o "planes" (que asientos habria que escribir). Eso lo hace
facil de testear: los tests de tests/test_reglas.py corren sin Postgres.

Toda la plata se maneja con Decimal, nunca con float. Con float, 0.1 + 0.2 no
da 0.3 y los centavos se pudren despues de unas cuantas operaciones.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

# Dos decimales: trabajamos en pesos con centavos.
CENTAVOS = Decimal("0.01")

# Los intereses se calculan sobre año de 365 dias (asi se cotizan las TNA).
DIAS_DEL_ANIO = Decimal(365)


def a_decimal(valor) -> Decimal:
    """
    Convierte lo que venga (int, str, float, Decimal) a un Decimal de plata.

    Pasamos por str() incluso cuando viene un float, porque Decimal(0.1) da
    0.1000000000000000055511151231257827021181583404541015625 mientras que
    Decimal("0.1") da exactamente 0.1.
    """
    if isinstance(valor, Decimal):
        return valor
    return Decimal(str(valor))


def redondear(monto) -> Decimal:
    """Redondea a centavos, con la regla de siempre (0.005 sube a 0.01)."""
    return a_decimal(monto).quantize(CENTAVOS, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Inversiones (plazo fijo)
# ---------------------------------------------------------------------------

def calcular_interes(capital, tna, plazo_dias: int) -> Decimal:
    """
    Interes simple de un plazo fijo: capital x (TNA / 365) x plazo_dias.

    `tna` va como fraccion decimal: 0.35 es 35% anual. Ojo con esto, es el
    error mas facil de cometer en todo el proyecto.

    No contempla retenciones ni impuestos: es TNA nominal y nada mas.
    """
    if plazo_dias <= 0:
        raise ValueError("El plazo tiene que ser de al menos un dia")

    capital = a_decimal(capital)
    tna = a_decimal(tna)
    interes = capital * (tna / DIAS_DEL_ANIO) * Decimal(plazo_dias)
    return redondear(interes)


def calcular_vencimiento(fecha_inicio: date, plazo_dias: int) -> date:
    """Fecha en la que se acredita el plazo fijo."""
    if plazo_dias <= 0:
        raise ValueError("El plazo tiene que ser de al menos un dia")
    return fecha_inicio + timedelta(days=plazo_dias)


def porcentaje_a_tna(porcentaje) -> Decimal:
    """
    Pasa "35" (como lo dice el usuario) a 0.35 (como lo guardamos).

    El interprete recibe el numero tal cual lo dijo la persona, y esta funcion
    es el unico lugar donde se hace la division. Si algun dia cambiamos de
    criterio, se cambia aca.
    """
    return a_decimal(porcentaje) / Decimal(100)


def tna_a_porcentaje(tna) -> Decimal:
    """La vuelta de porcentaje_a_tna, para mostrarle numeros al usuario."""
    return a_decimal(tna) * Decimal(100)


# ---------------------------------------------------------------------------
# Saldos
# ---------------------------------------------------------------------------

def sumar_montos(montos) -> Decimal:
    """
    Suma una lista de montos con signo. Es literalmente el saldo de un fondo.

    Vive aca (y no solo como un SUM() en SQL) para poder testear el criterio
    sin base de datos.
    """
    total = Decimal("0")
    for monto in montos:
        total += a_decimal(monto)
    return redondear(total)


# ---------------------------------------------------------------------------
# Consumo de una reserva
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanConsumo:
    """
    Que hay que escribir en el libro cuando se gasta contra una reserva.

    Cuando se creo la reserva ya salio del fondo un asiento de -monto_reserva.
    Al consumirla escribimos dos asientos:

      1. reserva_devuelta  +monto_reserva  (devolvemos TODO lo apartado)
      2. gasto             -monto_gasto    (y registramos el gasto real)

    El efecto neto sobre el saldo es -monto_gasto, que es exactamente lo que
    tiene que pasar en los dos casos del enunciado:

      - Gasto menor que la reserva: el sobrante vuelve al fondo solo.
      - Gasto mayor que la reserva: el excedente sale del fondo solo.

    Podriamos haber escrito un unico asiento por la diferencia y daria el mismo
    saldo, pero entonces el gasto no quedaria registrado como gasto y no se
    podria mirar por categoria despues. Preferimos dos asientos y un libro que
    se entienda leyendolo.
    """

    monto_reserva: Decimal
    monto_gasto: Decimal
    #: Cuanto de la reserva no se llego a usar (0 si el gasto fue mayor).
    sobrante: Decimal
    #: Cuanto tuvo que poner el fondo por encima de la reserva (0 si alcanzo).
    excedente: Decimal

    @property
    def hay_excedente(self) -> bool:
        """True cuando el gasto se paso de la reserva y hay que avisar."""
        return self.excedente > 0

    @property
    def efecto_en_saldo(self) -> Decimal:
        """
        Cuanto se mueve el saldo del fondo al aplicar este plan.

        Sirve para los tests y para explicarle al usuario que paso.
        """
        return redondear(self.monto_reserva - self.monto_gasto)


def planificar_consumo_reserva(monto_reserva, monto_gasto) -> PlanConsumo:
    """Decide como se cierra una reserva cuando se gasta contra ella."""
    monto_reserva = redondear(monto_reserva)
    monto_gasto = redondear(monto_gasto)

    if monto_gasto <= 0:
        raise ValueError("El gasto tiene que ser positivo")

    diferencia = monto_reserva - monto_gasto
    return PlanConsumo(
        monto_reserva=monto_reserva,
        monto_gasto=monto_gasto,
        sobrante=diferencia if diferencia > 0 else Decimal("0.00"),
        excedente=-diferencia if diferencia < 0 else Decimal("0.00"),
    )
