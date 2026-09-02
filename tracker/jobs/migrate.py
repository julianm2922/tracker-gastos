"""
Crea (o actualiza) las tablas en Postgres.

Se corre a mano una vez, al empezar:

    python -m tracker.jobs.migrate

Es idempotente: correrlo de nuevo no rompe nada ni borra datos.
"""

from tracker.store import db


def main() -> None:
    with db.conectar() as conn:
        db.aplicar_schema(conn)
        print("Esquema aplicado.")

        fondos = conn.execute("SELECT nombre FROM fondos ORDER BY nombre").fetchall()
        print("Fondos:", ", ".join(f["nombre"] for f in fondos))


if __name__ == "__main__":
    main()
