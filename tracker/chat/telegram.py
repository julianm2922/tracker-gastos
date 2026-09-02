"""
Cliente minimo de la API de Telegram.

Usamos polling (getUpdates), no webhooks: no hay servidor prendido esperando.
Telegram guarda los mensajes hasta 24hs de su lado, asi que el cron puede
pasar cada 15 o 30 minutos y buscarlos sin perder nada.

Lo unico delicado es el offset: getUpdates devuelve los mensajes a partir de
un update_id. Si no guardamos por donde ibamos (lo hace store/estado.py), cada
corrida volveria a leer lo mismo.
"""

import requests

from tracker import config

BASE = "https://api.telegram.org"

#: Cuanto esperamos a cada request de Telegram, en segundos.
TIMEOUT = 30


class ErrorDeTelegram(Exception):
    """Telegram contesto que algo salio mal."""


def _url(metodo: str) -> str:
    return f"{BASE}/bot{config.telegram_bot_token()}/{metodo}"


def _pedir(metodo: str, **params) -> dict:
    """Llama a un metodo de la API y devuelve el campo `result`."""
    respuesta = requests.post(_url(metodo), json=params, timeout=TIMEOUT)
    datos = respuesta.json()
    if not datos.get("ok"):
        raise ErrorDeTelegram(f"{metodo}: {datos.get('description')}")
    return datos["result"]


def obtener_yo() -> dict:
    """
    Datos del propio bot (metodo getMe).

    Sirve para confirmar que el token es correcto y, sobre todo, para saber el
    @usuario del bot: si le estas escribiendo al bot equivocado, getUpdates te
    va a devolver vacio para siempre.
    """
    return _pedir("getMe")


def obtener_updates(offset: int | None = None, limite: int = 100) -> list[dict]:
    """
    Trae los mensajes nuevos.

    `offset` es el update_id desde el cual leer. Ojo: pedirle a Telegram los
    updates con un offset tambien le confirma que los anteriores ya los
    procesamos, y los borra de su cola.

    timeout=0 es long polling apagado: preguntamos, contesta lo que hay y
    corta. En un cron es lo que queremos, no tiene sentido quedarse esperando.
    """
    params = {"limit": limite, "timeout": 0}
    if offset is not None:
        params["offset"] = offset
    return _pedir("getUpdates", **params)


def enviar_mensaje(
    chat_id: int, texto: str, responder_a: int | None = None
) -> dict:
    """
    Manda un mensaje y devuelve el mensaje enviado.

    Del resultado nos importa `message_id`: lo guardamos en el asiento o en el
    pendiente para poder resolver despues las respuestas por reply.
    """
    params = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "HTML",
        # Si el texto tiene un link, que no arme la vista previa gigante.
        "link_preview_options": {"is_disabled": True},
    }
    if responder_a is not None:
        params["reply_to_message_id"] = responder_a
        # Si el mensaje al que respondemos ya no existe, mandarlo igual.
        params["allow_sending_without_reply"] = True
    return _pedir("sendMessage", **params)


def descargar_archivo(file_id: str) -> bytes:
    """
    Baja un archivo (una foto o un audio) que mando el usuario.

    Son dos pasos: getFile devuelve una ruta interna, y despues se baja de
    /file/bot<TOKEN>/<ruta>.
    """
    archivo = _pedir("getFile", file_id=file_id)
    url = f"{BASE}/file/bot{config.telegram_bot_token()}/{archivo['file_path']}"
    respuesta = requests.get(url, timeout=TIMEOUT)
    respuesta.raise_for_status()
    return respuesta.content


# ---------------------------------------------------------------------------
# Lectura de los updates. Telegram devuelve JSON con muchisimos campos; estas
# funciones son para no andar escarbando diccionarios en el resto del codigo.
# ---------------------------------------------------------------------------

def es_del_chat_permitido(mensaje: dict, chat_id_permitido: int) -> bool:
    """
    El bot solo le hace caso a su dueño.

    Cualquiera que conozca el nombre del bot puede escribirle; sin este filtro,
    un desconocido podria cargar gastos en tu base.
    """
    return mensaje.get("chat", {}).get("id") == chat_id_permitido


def foto_mas_grande(mensaje: dict) -> dict | None:
    """
    Telegram manda cada foto en varios tamaños. Nos quedamos con el mas grande,
    que es el ultimo del array, porque es el que mejor se lee.
    """
    fotos = mensaje.get("photo")
    if not fotos:
        return None
    return fotos[-1]


def id_del_mensaje_respondido(mensaje: dict) -> int | None:
    """
    Si el mensaje es una respuesta a otro, devuelve el id de ese otro.

    Esto es lo que permite corregir sin ambiguedad: el usuario responde al
    mensaje del gasto y ya sabemos exactamente de cual habla.
    """
    respondido = mensaje.get("reply_to_message")
    return respondido["message_id"] if respondido else None
