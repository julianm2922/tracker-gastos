-- Esquema inicial de la base.
--
-- Idea central: los saldos NO se guardan, se calculan sumando asientos.
-- Un asiento es inmutable: nunca se hace UPDATE ni DELETE sobre uno existente.
-- Para anular algo se crea otro asiento que lo revierte.
--
-- Este archivo se puede correr las veces que quieras: todo usa
-- CREATE ... IF NOT EXISTS.

-- ---------------------------------------------------------------------------
-- fondos: de donde sale y a donde entra la plata ("sueldo", "ahorro", "bono").
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fondos (
    id        SERIAL PRIMARY KEY,
    nombre    TEXT NOT NULL UNIQUE,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- reservas: plata apartada dentro de un fondo para un gasto futuro concreto
-- ("la prepaga"). Apartarla genera un asiento negativo, asi que la reserva
-- sale del saldo disponible pero sigue siendo tuya.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reservas (
    id         SERIAL PRIMARY KEY,
    fondo_id   INTEGER NOT NULL REFERENCES fondos (id),
    concepto   TEXT NOT NULL,
    monto      NUMERIC(14, 2) NOT NULL CHECK (monto > 0),
    estado     TEXT NOT NULL DEFAULT 'activa'
               CHECK (estado IN ('activa', 'consumida', 'cancelada')),
    creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    cerrado_en TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS reservas_activas_idx
    ON reservas (fondo_id) WHERE estado = 'activa';


-- ---------------------------------------------------------------------------
-- inversiones: por ahora solo plazos fijos.
-- `tna` se guarda como fraccion decimal: 0.3500 es 35% anual.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inversiones (
    id                SERIAL PRIMARY KEY,
    fondo_id          INTEGER NOT NULL REFERENCES fondos (id),
    capital           NUMERIC(14, 2) NOT NULL CHECK (capital > 0),
    tna               NUMERIC(8, 4) NOT NULL,
    plazo_dias        INTEGER NOT NULL CHECK (plazo_dias > 0),
    fecha_inicio      DATE NOT NULL,
    fecha_vencimiento DATE NOT NULL,
    estado            TEXT NOT NULL DEFAULT 'activa'
                      CHECK (estado IN ('activa', 'acreditada', 'cancelada')),
    creado_en         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS inversiones_activas_idx
    ON inversiones (fecha_vencimiento) WHERE estado = 'activa';


-- ---------------------------------------------------------------------------
-- asientos: el libro. Cada fila es un movimiento inmutable.
-- `monto` va CON SIGNO: negativo = sale del fondo, positivo = entra.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asientos (
    id            SERIAL PRIMARY KEY,
    fondo_id      INTEGER NOT NULL REFERENCES fondos (id),
    monto         NUMERIC(14, 2) NOT NULL,
    tipo          TEXT NOT NULL CHECK (tipo IN (
                      'ingreso',
                      'gasto',
                      'reserva_apartada',
                      'reserva_devuelta',
                      'inversion_capital',
                      'inversion_retorno',
                      'reversion'
                  )),
    descripcion   TEXT,
    categoria     TEXT,
    fecha         DATE NOT NULL DEFAULT CURRENT_DATE,
    canal         TEXT NOT NULL DEFAULT 'telegram'
                  CHECK (canal IN ('telegram', 'mercadopago', 'sistema')),

    reserva_id    INTEGER REFERENCES reservas (id),
    inversion_id  INTEGER REFERENCES inversiones (id),

    -- Si esta seteado, este asiento anula al asiento apuntado.
    -- El UNIQUE es la garantia de que nada se puede anular dos veces.
    revierte_a_id INTEGER UNIQUE REFERENCES asientos (id),

    -- id de la operacion en Mercado Pago. El UNIQUE es lo que evita importar
    -- dos veces el mismo movimiento (dedupe).
    origen_ref    TEXT UNIQUE,

    -- Ids de Telegram, para poder resolver correcciones por "reply".
    telegram_message_id     BIGINT,
    telegram_bot_message_id BIGINT,

    creado_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS asientos_fondo_idx ON asientos (fondo_id);
CREATE INDEX IF NOT EXISTS asientos_fecha_idx ON asientos (fecha DESC);
CREATE INDEX IF NOT EXISTS asientos_tg_msg_idx ON asientos (telegram_message_id);
CREATE INDEX IF NOT EXISTS asientos_tg_bot_msg_idx ON asientos (telegram_bot_message_id);


-- ---------------------------------------------------------------------------
-- pendientes: preguntas que el bot hizo y todavia no tienen respuesta.
-- Un pendiente NO afecta ningun saldo hasta que se resuelve.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pendientes (
    id          SERIAL PRIMARY KEY,
    tipo        TEXT NOT NULL CHECK (tipo IN ('match_reserva', 'desambiguacion')),
    payload     JSONB NOT NULL,
    estado      TEXT NOT NULL DEFAULT 'esperando'
                CHECK (estado IN ('esperando', 'resuelto', 'descartado')),
    telegram_bot_message_id BIGINT,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    cerrado_en  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS pendientes_esperando_idx
    ON pendientes (telegram_bot_message_id) WHERE estado = 'esperando';


-- ---------------------------------------------------------------------------
-- estado_app: memoria chiquita entre corridas del cron.
-- Guarda, por ejemplo, el offset de getUpdates de Telegram: sin esto cada
-- corrida volveria a leer los mismos mensajes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS estado_app (
    clave          TEXT PRIMARY KEY,
    valor          TEXT NOT NULL,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- El fondo por defecto tiene que existir siempre.
INSERT INTO fondos (nombre) VALUES ('sueldo')
ON CONFLICT (nombre) DO NOTHING;
