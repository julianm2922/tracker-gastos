# Tracker

Sistema personal de finanzas que se maneja hablandole a un bot de Telegram, en
lenguaje natural, por texto, audio o foto de un comprobante. Ademas importa
solo los movimientos de Mercado Pago.

No hay ningun servidor prendido: todo corre en GitHub Actions con `cron`, y la
base es un Postgres de free tier. El unico gasto son los centavos de la API de
Claude.

```
"gaste 12000 de mi sueldo en la farmacia"
"reservo 420000 para la prepaga"
"puse 500000 a plazo fijo al 35% a 30 dias"
"cuanto tengo?"
```

Y respondiendo al mensaje del bot: `"che, eran 15000 no 12000"`.

## La idea central: los saldos no se guardan, se calculan

Cada operacion es un **asiento inmutable** con un monto con signo. El saldo de
un fondo es la suma de sus asientos, y nada mas.

- **Anular** no es borrar: es escribir otro asiento que revierte al primero.
- **Modificar** es anular y volver a crear.
- Nunca se hace `UPDATE` ni `DELETE` sobre un asiento.

Asi anular algo de hace tres meses no rompe nada, y siempre se puede ver por
que un fondo tiene el saldo que tiene.

Tres numeros por fondo:

```
saldo        = suma de los asientos           <- lo que realmente podes gastar
comprometido = suma de las reservas activas   <- apartado para algo
total        = saldo + comprometido
```

Apartar plata en una reserva genera un asiento negativo: por eso sale del saldo
pero sigue estando en el total.

## Como esta armado

```
tracker/
├── store/          la base: asientos, saldos, reservas, inversiones
│   ├── reglas.py       logica pura (intereses, consumo de reservas). Sin SQL.
│   ├── schema.sql      las tablas
│   └── *.py            una funcion por operacion, SQL a mano
├── interpreter/    Claude: texto o foto -> operacion estructurada
├── listener/       faster-whisper: audio -> texto
├── chat/           Telegram: polling, ruteo de operaciones, respuestas
├── movements/      Mercado Pago: traer movimientos y matchearlos
└── jobs/           los entrypoints que dispara el cron
```

Dos entrypoints:

| Job | Cada cuanto | Que hace |
|---|---|---|
| `tracker.jobs.sync_telegram` | 20 min | Lee los mensajes nuevos y los registra |
| `tracker.jobs.sync_mercadopago` | 1 vez por dia | Importa movimientos de MP y acredita plazos fijos vencidos |

El polling funciona sin perder mensajes porque Telegram los guarda de su lado
hasta 24 horas.

## Reglas de negocio

| Situacion | Que pasa |
|---|---|
| Operacion sin fondo | Sale de `sueldo` |
| Crear una reserva | Asiento negativo; la reserva queda `activa` |
| Cancelar una reserva | Vuelve todo al fondo; queda `cancelada` |
| Gasto **menor** a la reserva | Se registra el gasto, el sobrante vuelve al fondo; `consumida` |
| Gasto **mayor** a la reserva | Se registra el gasto, la diferencia sale del fondo y el bot avisa; `consumida` |
| Crear un plazo fijo | Asiento negativo por el capital; queda `activa` |
| Plazo fijo vencido | El cron acredita capital + interes en el fondo de origen y avisa; `acreditada` |
| Pago de MP que parece de una reserva | Queda `pendiente`: el bot pregunta y no toca ningun saldo |
| Anular o modificar | Asiento de reversion, nunca `DELETE` |
| Fondo en negativo | Se permite, solo avisa |

Interes: `capital x (TNA / 365) x plazo_dias`, TNA nominal, sin retenciones ni
impuestos. La TNA se **guarda como fraccion** (`0.35` es 35%); la conversion
esta en un solo lugar (`reglas.porcentaje_a_tna`).

## Puesta en marcha

### 1. La base de datos

Crear un proyecto en [Neon](https://neon.tech) o
[Supabase](https://supabase.com) (los dos tienen free tier sin vencimiento) y
copiar la cadena de conexion.

En Supabase, del boton **Connect** hay que tomar la del **Session pooler**, no
la de *Direct connection*: la directa (`db.<ref>.supabase.co`) resuelve solo
por IPv6 y los runners de GitHub Actions son IPv4, asi que los jobs no podrian
conectarse. La del pooler es:

```
postgresql://postgres.<ref>:<clave>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

Y hay que reemplazar el `[YOUR-PASSWORD]` que viene en la cadena por la
contraseña de verdad.

### 2. El bot de Telegram

Hablarle a [@BotFather](https://t.me/BotFather), `/newbot`, y guardar el token
en el `.env`. Despues, para averiguar el chat id:

```bash
.venv/bin/python -m tracker.jobs.chat_id
```

El comando queda esperando: abris Telegram, le mandas cualquier mensaje al bot
y te imprime el id para pegar en el `.env`.

El chat id no esta en ningun panel y no se puede consultar: aparece recien
cuando hay un mensaje, porque viene adentro del mensaje. Por eso
`https://api.telegram.org/bot<TOKEN>/getUpdates` devuelve `result: []` si
todavia no le escribiste al bot.

### 3. Local

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-audio.txt   # sin audio: requirements.txt

cp .env.example .env    # y completar los valores

.venv/bin/python -m tracker.jobs.migrate          # crea las tablas
.venv/bin/python -m tracker.jobs.sync_telegram    # una corrida a mano
```

### 4. GitHub Actions

En **Settings > Secrets and variables > Actions**, cargar como *secrets*:

```
ANTHROPIC_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_CHAT_ID
DATABASE_URL
MERCADOPAGO_ACCESS_TOKEN
```

Opcionalmente, como *variable*: `WHISPER_MODEL` (por defecto `small`).

Los dos workflows arrancan solos. Tambien se pueden disparar a mano desde la
pestaña **Actions** (`Run workflow`), que es la forma comoda de probarlos.

> Ojo: GitHub deshabilita los workflows programados de un repo despues de 60
> dias sin actividad. Un commit cada tanto (o entrar y reactivarlos) alcanza.

## Tests

```bash
.venv/bin/python -m pytest
```

Sin configuracion corren los tests que no necesitan nada: reglas de negocio,
interprete (con un cliente falso), parseo del reporte de MP y formato de
mensajes.

Para correr tambien los que van contra Postgres hace falta una base
**descartable**: los tests borran todas las tablas antes de cada uno.
**Nunca apuntes `TEST_DATABASE_URL` a la base que usas de verdad.**

Lo mas comodo es levantarla con Docker:

```bash
docker compose up -d --wait

TEST_DATABASE_URL=postgresql://tracker:tracker@localhost:5433/tracker_test \
    .venv/bin/python -m pytest

docker compose down -v
```

Tambien sirve una base remota (en Neon, una branch aparte; en Supabase, otro
proyecto):

```bash
TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest
```

> El `docker-compose.yml` es solo para esto. El proyecto no se despliega con
> Docker: los jobs corren en GitHub Actions, que ya da un entorno limpio en
> cada corrida.

## Decisiones que conviene conocer

**Un gasto contra una reserva escribe dos asientos**, no uno: uno que libera
toda la reserva y otro que registra el gasto. El efecto sobre el saldo es el
mismo que escribir solo la diferencia, pero de esta forma el gasto queda
registrado como gasto y se puede mirar por categoria despues.

**Hay una tabla `estado_app`** que no estaba en el diseño original. Guarda el
offset de `getUpdates`: sin eso, cada corrida del cron volveria a leer los
mismos mensajes de Telegram y duplicaria todo.

**Las inversiones tienen un estado `cancelada`** ademas de `activa` y
`acreditada`, para cuando se anula el asiento que creo el plazo fijo. Sin eso,
el cron seguiria esperando el vencimiento de algo que ya no existe.

**Hay una tool `no_entendido`.** Como a Claude se lo obliga a llamar a alguna
tool siempre (`tool_choice: any`), sin esta salida inventaria un gasto ante un
mensaje ambiguo.

**El reporte de Mercado Pago es la parte mas fragil.** Los nombres de las
columnas del CSV cambian segun el tipo de cuenta y la version del reporte, asi
que `movements/mercadopago.py` busca cada dato entre varios nombres posibles y
descarta las filas que no entiende. Si algun movimiento no aparece, mirar las
constantes `COLUMNAS_*` de ese modulo contra el CSV real.

## Costos

| Que | Cuanto |
|---|---|
| GitHub Actions | Gratis en repos publicos; en privados, dentro de los 2000 min/mes |
| Postgres (Neon / Supabase) | Free tier |
| Transcripcion de audio | Gratis: whisper corre en el runner |
| API de Claude | Haiku, un par de miles de tokens por mensaje |

## Lo que todavia no hace

Dashboard, graficos, proyecciones, multiusuario, otros canales ademas de
Telegram, instrumentos de inversion que no sean plazo fijo, y retenciones o
ajuste por inflacion en el rendimiento.
