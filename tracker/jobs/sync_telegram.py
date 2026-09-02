"""
Flujo A: leer Telegram y registrar lo que haya.

Lo dispara el cron cada 15-30 minutos:

    python -m tracker.jobs.sync_telegram

Por cada mensaje:

    getUpdates -> audio/texto/foto -> Claude -> store -> respuesta por Telegram

Dos detalles que hacen que esto funcione sin servidor:

- El offset de getUpdates se guarda en la base. Sin eso, cada corrida
  reprocesaria los mismos mensajes.
- Se hace commit despues de cada mensaje. Si el quinto falla, los cuatro
  primeros quedan guardados igual.
"""

import traceback

from tracker import config, fechas
from tracker.chat import router, telegram
from tracker.interpreter import claude
from tracker.store import (
    asientos,
    db,
    estado,
    fondos,
    inversiones,
    pendientes,
    reservas,
)


def armar_contexto(conn, mensaje: dict) -> tuple[claude.Contexto, dict | None, dict | None]:
    """
    Junta todo lo que Claude necesita saber ademas del mensaje.

    Devuelve (contexto, asiento_referido, pendiente), porque el router tambien
    necesita esos dos ultimos para saber sobre que operar.
    """
    asiento_referido = None
    pendiente = None

    respondido = telegram.id_del_mensaje_respondido(mensaje)
    if respondido is not None:
        # El usuario respondio a un mensaje: puede ser la confirmacion de una
        # operacion (quiere corregirla) o una pregunta del bot (la contesta).
        asiento_referido = asientos.buscar_por_mensaje_telegram(conn, respondido)
        pendiente = pendientes.buscar_por_mensaje_del_bot(conn, respondido)

    if pendiente is None:
        # Sin reply, si hay una sola pregunta abierta asumimos que es esa: es
        # un sistema de un solo usuario, no hay con quien confundirse.
        pendiente = pendientes.ultimo_esperando(conn)

    contexto = claude.Contexto(
        hoy=fechas.hoy(),
        fondos=[f["nombre"] for f in fondos.listar(conn)],
        reservas_activas=reservas.listar_activas(conn),
        inversiones_activas=inversiones.listar_activas(conn),
        asiento_referido=asiento_referido,
        pendiente=pendiente,
    )
    return contexto, asiento_referido, pendiente


def extraer_contenido(mensaje: dict) -> tuple[str | None, bytes | None]:
    """
    Saca del mensaje el texto y/o la imagen para mandarle a Claude.

    - Texto: se usa tal cual.
    - Nota de voz o audio: se transcribe con whisper.
    - Foto: se baja y va como imagen (Claude la interpreta; no hay OCR).
      El epigrafe, si lo hay, se manda junto.
    """
    texto = mensaje.get("text") or mensaje.get("caption")
    imagen = None

    audio = mensaje.get("voice") or mensaje.get("audio")
    if audio:
        # Se importa aca adentro porque faster-whisper tarda en cargar y no
        # tiene sentido pagar eso en los mensajes que son solo texto.
        from tracker.listener import audio as listener

        crudo = telegram.descargar_archivo(audio["file_id"])
        transcripcion = listener.transcribir(crudo)
        texto = f"{texto}\n{transcripcion}" if texto else transcripcion

    foto = telegram.foto_mas_grande(mensaje)
    if foto:
        imagen = telegram.descargar_archivo(foto["file_id"])

    return texto, imagen


def procesar_mensaje(conn, mensaje: dict) -> None:
    """Interpreta un mensaje, lo ejecuta y contesta."""
    chat_id = mensaje["chat"]["id"]
    message_id = mensaje["message_id"]

    texto, imagen = extraer_contenido(mensaje)
    if texto is None and imagen is None:
        telegram.enviar_mensaje(
            chat_id,
            "Puedo con texto, notas de voz y fotos de comprobantes. Eso no lo se leer.",
            responder_a=message_id,
        )
        return

    contexto, asiento_referido, pendiente = armar_contexto(conn, mensaje)

    operacion = claude.interpretar(texto, contexto=contexto, imagen=imagen)
    respuesta = router.ejecutar(
        conn,
        operacion,
        telegram_message_id=message_id,
        asiento_referido=asiento_referido,
        pendiente=pendiente,
    )

    enviado = telegram.enviar_mensaje(chat_id, respuesta.texto, responder_a=message_id)

    # Guardamos a que mensaje del bot quedo atada la respuesta. Esto es lo que
    # permite que despues la persona conteste "eran 15000" haciendo reply.
    if respuesta.asiento_id:
        asientos.marcar_mensaje_del_bot(conn, respuesta.asiento_id, enviado["message_id"])
    if respuesta.pendiente_id:
        pendientes.marcar_mensaje_del_bot(
            conn, respuesta.pendiente_id, enviado["message_id"]
        )


def main() -> None:
    chat_permitido = config.telegram_allowed_chat_id()

    with db.conectar() as conn:
        offset = estado.obtener_offset_telegram(conn)
        updates = telegram.obtener_updates(offset)
        print(f"{len(updates)} mensajes nuevos")

        for update in updates:
            mensaje = update.get("message") or update.get("edited_message")

            if mensaje and telegram.es_del_chat_permitido(mensaje, chat_permitido):
                try:
                    procesar_mensaje(conn, mensaje)
                except Exception:
                    # Un mensaje que falla no puede trabar la cola: se avisa,
                    # se descarta y se sigue con el siguiente.
                    conn.rollback()
                    traceback.print_exc()
                    telegram.enviar_mensaje(
                        chat_permitido,
                        "Se me complico procesando ese mensaje. Probá de nuevo "
                        "o escribilo de otra forma.",
                        responder_a=mensaje["message_id"],
                    )

            # El offset avanza SIEMPRE, incluso si el mensaje fallo o era de
            # otro chat. Si no, un mensaje problematico bloquearia todo para
            # siempre.
            estado.guardar_offset_telegram(conn, update["update_id"] + 1)
            conn.commit()


if __name__ == "__main__":
    main()
