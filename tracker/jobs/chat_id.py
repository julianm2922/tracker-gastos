"""
Averigua tu chat id de Telegram, que es lo unico que no se puede sacar de
ningun panel.

    python -m tracker.jobs.chat_id

Necesita TELEGRAM_BOT_TOKEN en el .env. El script se queda esperando: mientras
corre, abri Telegram, buscá el bot por el @usuario que te muestra y mandale
cualquier mensaje.

Por que hace falta esto: Telegram no tiene una API para preguntar "cual es mi
chat id". El id aparece recien cuando hay un mensaje, porque viene adentro del
mensaje. Si nunca le escribiste al bot, getUpdates devuelve una lista vacia y
no hay nada que mirar.

Este script NO consume los mensajes: pide los updates sin offset, que es una
lectura. El primero que se los lleva de la cola es el job de verdad
(sync_telegram), y recien cuando el chat id ya este configurado.
"""

import time

from tracker.chat import telegram

#: Cuantas veces mira, y cada cuanto. 60 x 5s = cinco minutos de espera.
INTENTOS = 60
ESPERA = 5


def chats_en(updates: list[dict]) -> dict[int, str]:
    """Saca los chats distintos que aparecen en los updates."""
    encontrados = {}
    for update in updates:
        mensaje = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
        )
        if not mensaje:
            continue
        chat = mensaje.get("chat", {})
        if "id" not in chat:
            continue
        # El nombre es solo para que sepas cual es cual si hay varios.
        nombre = chat.get("username") or chat.get("first_name") or chat.get("title", "")
        encontrados[chat["id"]] = f"{nombre} ({chat.get('type', '?')})"
    return encontrados


def main() -> None:
    bot = telegram.obtener_yo()
    print(f"Bot: {bot.get('first_name')} (@{bot.get('username')})")
    print()
    print(f"Abri Telegram, buscá @{bot.get('username')} y mandale un mensaje.")
    print("Te espero...")
    print()

    for intento in range(INTENTOS):
        chats = chats_en(telegram.obtener_updates())

        if chats:
            print("Listo. Chats encontrados:")
            for chat_id, quien in chats.items():
                print(f"    {chat_id}   <- {quien}")
            print()
            if len(chats) == 1:
                unico = next(iter(chats))
                print("Copiá esto al .env:")
                print(f"    TELEGRAM_ALLOWED_CHAT_ID={unico}")
            else:
                print("Hay varios: elegi el de tu chat privado con el bot.")
            return

        if intento < INTENTOS - 1:
            time.sleep(ESPERA)

    print("No llego ningun mensaje. Cosas para revisar:")
    print("  - Que le estes escribiendo a @%s y no a otro bot." % bot.get("username"))
    print("  - Que le hayas dado a INICIAR / START la primera vez.")
    print("  - Que el mensaje sea de hace menos de 24hs (Telegram no guarda mas).")


if __name__ == "__main__":
    main()
