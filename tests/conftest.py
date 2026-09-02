"""
Configuracion compartida de los tests.

Hay dos clases de test en este proyecto:

- Los que no tocan la base (reglas, interprete, parseo de MP, mensajes):
  corren siempre, en cualquier maquina y en cada push.
- Los de los archivos test_*_postgres.py: prueban el store de punta a punta
  contra un Postgres de verdad.

Los segundos necesitan una base, que sale de TEST_DATABASE_URL o, si no esta,
de DATABASE_URL. Si no hay ninguna, se saltean.

Que apunten a la base de verdad no es un problema porque cada test corre en su
propio schema temporal, que se crea al empezar y se borra al terminar: el
schema `public`, donde estan tus datos, no se toca nunca.
"""

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

# Para poder importar `tracker` sin instalar el paquete.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def conn():
    """
    Conexion a un schema descartable, con el esquema del proyecto recien
    aplicado.

    Un schema nuevo por test: cada uno arranca de cero y son independientes
    entre si, que es lo que mas importa en una suite de este tamaño.
    """
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("No hay TEST_DATABASE_URL ni DATABASE_URL; se saltean")

    from tracker.store import db

    # Nombre unico para que dos corridas en paralelo no se pisen.
    schema = f"tests_{uuid4().hex[:12]}"

    with db.conectar(url) as conexion:
        conexion.execute(f'CREATE SCHEMA "{schema}"')
        # Sin `public` en el search_path: asi todo lo que hagan los tests
        # (crear tablas, insertar, consultar) resuelve contra el schema
        # temporal y no hay forma de tocar los datos de verdad por accidente.
        conexion.execute(f'SET search_path TO "{schema}"')
        db.aplicar_schema(conexion)
        # El commit deja fijado el search_path para el resto de la sesion; un
        # rollback lo revertiria junto con el resto de la transaccion.
        conexion.commit()

        try:
            yield conexion
        finally:
            conexion.rollback()
            conexion.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conexion.commit()
