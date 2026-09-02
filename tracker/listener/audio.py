"""
Transcripcion de audios con faster-whisper, corriendo local en el Action.

Se usa el modelo 'small' en español y en CPU: en la maquina que da GitHub
Actions gratis alcanza de sobra para notas de voz de unos segundos, y no
cuesta nada. El modelo se baja una vez y queda cacheado (ver el workflow), asi
no se descarga en cada corrida.

faster-whisper es pesado de importar (arrastra CTranslate2), asi que se
importa adentro de la funcion. De ese modo el job de Mercado Pago, que no
transcribe nada, no paga ese costo, y los tests tampoco necesitan tenerlo
instalado.
"""

import tempfile
from pathlib import Path

from tracker import config

#: El modelo se carga una sola vez por corrida y se reusa.
_modelo = None


def _obtener_modelo():
    """Carga el modelo de whisper (la primera vez tarda; despues es instantaneo)."""
    global _modelo
    if _modelo is None:
        from faster_whisper import WhisperModel

        _modelo = WhisperModel(
            config.whisper_model(),
            device="cpu",
            # int8 es cuantizacion: usa menos memoria y va bastante mas rapido
            # en CPU, a cambio de una perdida de precision que para dictar
            # numeros y nombres de comercios no se nota.
            compute_type="int8",
            download_root=config.whisper_cache_dir(),
        )
    return _modelo


def transcribir(audio: bytes, extension: str = ".ogg") -> str:
    """
    Convierte un audio en texto.

    Telegram manda las notas de voz en OGG/Opus. faster-whisper decodifica el
    audio con PyAV, que ya viene incluido (no hace falta instalar ffmpeg), pero
    espera una ruta a un archivo: de ahi el archivo temporal.
    """
    with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as archivo:
        archivo.write(audio)
        ruta = Path(archivo.name)

    try:
        segmentos, _info = _obtener_modelo().transcribe(
            str(ruta),
            language="es",
            # Saltea los silencios: en una nota de voz con pausas, evita que el
            # modelo invente texto donde no hay nada.
            vad_filter=True,
        )
        # `segmentos` es un generador: recien al recorrerlo se hace el trabajo.
        return " ".join(segmento.text.strip() for segmento in segmentos).strip()
    finally:
        ruta.unlink(missing_ok=True)
