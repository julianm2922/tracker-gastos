"""
Fechas. Chiquito pero importante.

El cron de GitHub Actions corre en UTC. Si preguntaramos date.today() sin mas,
un gasto cargado a las 22hs de un martes en Buenos Aires se guardaria como
miercoles. Por eso todo el proyecto pide la fecha por aca.
"""

from datetime import date, datetime

from tracker.config import ZONA_HORARIA


def hoy() -> date:
    """La fecha de hoy en Buenos Aires."""
    return datetime.now(ZONA_HORARIA).date()


def parsear(texto: str | None) -> date | None:
    """
    Convierte 'AAAA-MM-DD' a un date. Devuelve None si no vino o si esta mal.

    Lo que manda Claude puede venir raro; preferimos caer en "hoy" (que es lo
    que hace quien llama cuando recibe None) antes que romper el mensaje.
    """
    if not texto:
        return None
    try:
        return date.fromisoformat(texto.strip()[:10])
    except (ValueError, AttributeError):
        return None
