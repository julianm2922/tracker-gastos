"""
jobs: los entrypoints que dispara el cron de GitHub Actions.

- migrate.py:           crea las tablas (se corre a mano, una vez).
- chat_id.py:           averigua tu chat id de Telegram (a mano, una vez).
- sync_telegram.py:     flujo A, cada 20 minutos.
- sync_mercadopago.py:  flujo B, una vez por dia.
"""
