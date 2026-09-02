"""
Configuracion del proyecto: todo lo que se lee del entorno vive aca.

La idea es que ningun otro modulo llame a os.environ directamente. Asi hay un
solo lugar donde mirar cuando algo falta, y los mensajes de error son claros.

En local las variables salen del archivo .env (ver .env.example).
En GitHub Actions salen de los Secrets del repositorio.
"""

import os
import re
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Carga el .env si existe. En GitHub Actions no existe y no pasa nada:
# load_dotenv() simplemente no hace nada y las variables vienen del entorno.
load_dotenv()


# Zona horaria con la que trabajamos. Importa porque el cron de GitHub corre en
# UTC: si no la fijamos, un gasto de las 22hs de un martes se guardaria como
# miercoles.
ZONA_HORARIA = ZoneInfo("America/Argentina/Buenos_Aires")

# Fondo que se usa cuando el usuario no aclara de donde sale la plata.
FONDO_POR_DEFECTO = "sueldo"

# Modelo de Claude que interpreta los mensajes. Haiku alcanza de sobra para
# esta tarea (clasificar una frase corta en una operacion) y es el mas barato.
MODELO_CLAUDE = "claude-haiku-4-5"


class FaltaConfiguracion(Exception):
    """Se lanza cuando falta una variable de entorno obligatoria."""


def _requerida(nombre: str) -> str:
    """
    Devuelve la variable de entorno `nombre` o explota con un mensaje util.

    Distingue dos casos que parecen el mismo pero se arreglan distinto:

    - No esta definida: nadie la puso. En local falta en el .env; en GitHub
      Actions falta en el bloque `env:` del workflow.
    - Definida pero vacia: esto en Actions significa casi siempre que el
      secret no existe. `${{ secrets.LO_QUE_SEA }}` no falla cuando el secret
      no esta: se expande a string vacio, y el job arranca igual.
    """
    valor = os.environ.get(nombre)

    if valor is None:
        raise FaltaConfiguracion(
            f"{nombre} no esta definida. En local: agregala al .env "
            f"(ver .env.example). En GitHub Actions: falta en el bloque `env:` "
            f"del paso que corre este job."
        )

    if not valor.strip():
        raise FaltaConfiguracion(
            f"{nombre} llego vacia. En GitHub Actions esto pasa cuando el "
            f"secret no existe con ese nombre exacto. Revisá: que este en "
            f"Settings > Secrets and variables > *Actions* (no en Dependabot "
            f"ni Codespaces), que sea un Secret y no un Variable, que este en "
            f"este repositorio y no en otro, y que no sea un secret de "
            f"Environment (esos necesitan `environment:` en el job)."
        )

    # Un secret pegado desde el navegador se lleva a veces un salto de linea
    # al final; sin este strip, un token valido falla por un caracter invisible.
    return valor.strip()


def _opcional(nombre: str, por_defecto: str = "") -> str:
    """Devuelve la variable de entorno `nombre`, o `por_defecto` si no esta."""
    return os.environ.get(nombre) or por_defecto


# Cada una de estas es una funcion y no una constante a proposito: si fueran
# constantes, importar este modulo sin tener el .env armado explotaria aunque
# la variable no haga falta para lo que estas haciendo.

def anthropic_api_key() -> str:
    return _requerida("ANTHROPIC_API_KEY")


def telegram_bot_token() -> str:
    return _requerida("TELEGRAM_BOT_TOKEN")


def telegram_allowed_chat_id() -> int:
    return int(_requerida("TELEGRAM_ALLOWED_CHAT_ID"))


def database_url() -> str:
    """
    Cadena de conexion a Postgres.

    Ademas de chequear que exista, verifica que no haya quedado el placeholder
    que Supabase pone en la cadena que te copia del panel: pegarla sin
    reemplazar la contraseña es facil de hacer y el error que da despues
    (falla la autenticacion) no dice de donde viene.
    """
    url = _requerida("DATABASE_URL")
    if re.search(r"://[^:/@]+:\[[^\]]*\]@", url):
        raise FaltaConfiguracion(
            "DATABASE_URL todavia tiene el placeholder de la contraseña "
            "(algo entre corchetes, como [YOUR-PASSWORD]). Reemplazalo por la "
            "contraseña real de la base."
        )
    return url


def mercadopago_access_token() -> str:
    return _requerida("MERCADOPAGO_ACCESS_TOKEN")


def whisper_model() -> str:
    return _opcional("WHISPER_MODEL", "small")


def whisper_cache_dir() -> str:
    return _opcional("WHISPER_CACHE_DIR", "modelos_whisper")
