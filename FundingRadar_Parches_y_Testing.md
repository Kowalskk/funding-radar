# FundingRadar — Parches + Guía de Testing

## Resumen de lo que se parchea

| # | Archivo | Qué se arregla |
|---|---------|----------------|
| 1 | `Dockerfile` | No copiaba alembic, scripts ni tests al container |
| 2 | `requirements.txt` | Quita `psycopg2-binary` (no se usa, todo es asyncpg) |
| 3 | `app/config.py` | Quita DEXs que no existen (dYdX, GMX, Drift), añade Aster |
| 4 | `.env.example` | Mismo cleanup + LOG_FORMAT=text para dev |
| 5 | `app/processors/arbitrage_calculator.py` | **Bug gordo**: annualizaba fees ×1095 como si se pagaran cada 8h, cuando son one-time entry/exit |
| 6 | `scripts/seed_exchanges.py` | Hyperliquid usa symbol "BTC", Aster usa "BTCUSDT" — ahora condicional |

Archivos nuevos:
- `.env` — listo para usar en local (solo hacer `docker compose up`)
- `scripts/test_backend.sh` — smoke test automático de 11 fases

---

## OPCIÓN A: Aplicar con git apply (recomendado)

Descarga el archivo `funding-radar-fixes.patch` que te adjunto y:

```bash
cd funding-radar
git apply funding-radar-fixes.patch
```

Luego copia los 2 archivos nuevos manualmente (`.env` y `scripts/test_backend.sh`) que también están adjuntos abajo.

---

## OPCIÓN B: Aplicar a mano (copy-paste)

### PATCH 1 — Dockerfile

Busca esta línea:
```dockerfile
# Copy application source
COPY app/ ./app/
```

Reemplázala por:
```dockerfile
# Copy application source
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY pytest.ini .
```

### PATCH 2 — requirements.txt

Borra esta línea:
```
psycopg2-binary==2.9.10
```

### PATCH 3 — app/config.py

Busca este bloque (líneas ~67-75):
```python
    # ── DEX API Endpoints ─────────────────────────────────
    dydx_api_url: str = "https://api.dydx.exchange"
    dydx_ws_url: str = "wss://api.dydx.exchange/v3/ws"
    hyperliquid_api_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    gmx_api_url: str = "https://stats.gmx.io/api"
    drift_api_url: str = "https://drift-historical-data.s3.eu-west-1.amazonaws.com"
```

Reemplázalo por:
```python
    # ── DEX API Endpoints ─────────────────────────────────
    hyperliquid_api_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    aster_api_url: str = "https://fapi.asterdex.com"
    aster_ws_url: str = "wss://fstream.asterdex.com"
```

### PATCH 4 — .env.example

Busca el bloque de DEX Integrations (líneas ~51-65):
```env
# ── DEX Integrations ──────────────────────────────────────
# dYdX
DYDX_API_URL=https://api.dydx.exchange
DYDX_WS_URL=wss://api.dydx.exchange/v3/ws

# Hyperliquid
HYPERLIQUID_API_URL=https://api.hyperliquid.xyz
HYPERLIQUID_WS_URL=wss://api.hyperliquid.xyz/ws

# GMX (Arbitrum)
GMX_API_URL=https://stats.gmx.io/api

# Drift (Solana)
DRIFT_API_URL=https://drift-historical-data.s3.eu-west-1.amazonaws.com
```

Reemplázalo por:
```env
# ── DEX Integrations ──────────────────────────────────────
# Hyperliquid
HYPERLIQUID_API_URL=https://api.hyperliquid.xyz
HYPERLIQUID_WS_URL=wss://api.hyperliquid.xyz/ws

# Aster
ASTER_API_URL=https://fapi.asterdex.com
ASTER_WS_URL=wss://fstream.asterdex.com
```

Y al final del archivo, cambia:
```env
LOG_LEVEL=INFO
LOG_FORMAT=json
```
Por:
```env
LOG_LEVEL=DEBUG
LOG_FORMAT=text
```

### PATCH 5 — app/processors/arbitrage_calculator.py (BUG CRÍTICO)

Busca este bloque (líneas ~142-155):
```python
            # ── Net APR — maker ───────────────────────────────────────────────
            # Fee cost annualised: paid once at entry + once at exit per leg
            # Annualisation proxy: treat as if positions are rolled every year
            # Cost (%) = (maker_long + maker_short) × 2 (entry+exit each leg)
            maker_fee_pct = (ld.maker_fee + sd.maker_fee) * 2        # total round trip %
            taker_fee_pct = (ld.taker_fee + sd.taker_fee) * 2        # total round trip %

            # Annualise the round-trip fee as an equivalent APR
            # Standard approach: fee_apr = fee_pct * 365 * 3  (8h period = 3×/day)
            maker_fee_apr = maker_fee_pct * 3 * 365
            taker_fee_apr = taker_fee_pct * 3 * 365

            net_apr_maker = funding_delta_apr - maker_fee_apr
            net_apr_taker = funding_delta_apr - taker_fee_apr
```

Reemplázalo por:
```python
            # ── Net APR — fees are one-time costs (entry + exit), NOT recurring ──
            # Each leg incurs a fee on entry and on exit.
            # Round-trip cost per leg = fee_rate × 2 (open + close)
            # Total round-trip cost across both legs:
            maker_fee_pct = (ld.maker_fee + sd.maker_fee) * 2        # total round trip %
            taker_fee_pct = (ld.taker_fee + sd.taker_fee) * 2        # total round trip %

            # Net APR = gross funding spread APR minus one-time fee cost.
            # The fee is a flat drag, not annualised — it's paid once regardless
            # of how long you hold. We subtract it as-is from the gross APR so
            # the resulting number tells the user "this is your APR after entry
            # and exit fees are accounted for over one full year of holding".
            net_apr_maker = funding_delta_apr - maker_fee_pct
            net_apr_taker = funding_delta_apr - taker_fee_pct
```

### PATCH 6 — scripts/seed_exchanges.py

Busca este bloque (líneas ~105-109):
```python
            for sym in token_symbols:
                tok = await session.scalar(select(Token).where(Token.symbol == sym))
                if tok is None:
                    continue
                stmt = insert(ExchangeToken).values(
                    exchange_id=ex.id, token_id=tok.id, exchange_symbol=f"{sym}USDT"
                ).on_conflict_do_nothing()
```

Reemplázalo por:
```python
            for sym in token_symbols:
                tok = await session.scalar(select(Token).where(Token.symbol == sym))
                if tok is None:
                    continue
                # Hyperliquid uses bare symbol "BTC"; Aster uses "BTCUSDT"
                if ex_slug == "hyperliquid":
                    ex_symbol = sym
                else:
                    ex_symbol = f"{sym}USDT"
                stmt = insert(ExchangeToken).values(
                    exchange_id=ex.id, token_id=tok.id, exchange_symbol=ex_symbol
                ).on_conflict_do_nothing()
```

---

## ARCHIVO NUEVO — .env (copia en la raíz del repo)

```env
# ─────────────────────────────────────────────────────────
#  funding-radar — Local Development Environment
# ─────────────────────────────────────────────────────────

APP_NAME=funding-radar
APP_ENV=development
APP_DEBUG=true
APP_HOST=0.0.0.0
APP_PORT=8000
APP_SECRET_KEY=dev-secret-key-change-in-production-32chars
ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8000,http://localhost:5173

POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=funding_user
POSTGRES_PASSWORD=devpass123
POSTGRES_DB=funding_radar
DATABASE_URL=postgresql+asyncpg://funding_user:devpass123@postgres:5432/funding_radar
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20
DATABASE_POOL_TIMEOUT=30

REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=redisdev123
REDIS_DB=0
REDIS_URL=redis://:redisdev123@redis:6379/0
REDIS_MAX_CONNECTIONS=20
CACHE_TTL_SECONDS=30

JWT_SECRET_KEY=jwt-dev-secret-change-in-production-needs-to-be-long-64chars-minimum
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=1440
JWT_REFRESH_TOKEN_EXPIRE_DAYS=30

HYPERLIQUID_API_URL=https://api.hyperliquid.xyz
HYPERLIQUID_WS_URL=wss://api.hyperliquid.xyz/ws
ASTER_API_URL=https://fapi.asterdex.com
ASTER_WS_URL=wss://fstream.asterdex.com

SCHEDULER_FUNDING_RATE_INTERVAL_SECONDS=10
SCHEDULER_CACHE_CLEANUP_INTERVAL_MINUTES=60

LOG_LEVEL=DEBUG
LOG_FORMAT=text
```

---

## Guía de Testing: Paso a Paso

### 1. Aplicar parches y levantar

```bash
cd funding-radar

# Aplicar parches (usa la opción A o B de arriba)

# Construir + levantar
docker compose up -d --build

# Ver logs en tiempo real
docker compose logs -f app
```

### 2. Migrations + Seed

```bash
# Espera a que postgres esté healthy (10-20 seg)
docker compose exec app alembic upgrade head

# Seed exchanges y tokens
docker compose exec app python scripts/seed_exchanges.py
```

### 3. Smoke test automático

```bash
# Espera ~60 segundos para que los collectors carguen datos
# Luego corre el test:
bash scripts/test_backend.sh
```

El script testea 11 fases automáticamente:
1. Health checks
2. Monitoring endpoints
3. Redis data
4. Funding rate API
5. Arbitrage API
6. Exchanges API
7. Simulator API
8. Auth (register + login + JWT + API key)
9. WebSocket
10. Database persistence
11. Historical data

### 4. Testing manual con curl

```bash
# ── Funding rates ──
curl -s http://localhost:8000/api/v1/funding/rates | python3 -m json.tool | head -40

# ── Arbitrage ──
curl -s http://localhost:8000/api/v1/arbitrage/opportunities | python3 -m json.tool

# ── Token detail ──
curl -s http://localhost:8000/api/v1/funding/token/BTC | python3 -m json.tool

# ── Simulator ──
curl -s -X POST http://localhost:8000/api/v1/simulator/calculate \
  -H "Content-Type: application/json" \
  -d '{"token":"BTC","long_exchange":"hyperliquid","short_exchange":"aster","capital_usd":10000,"days":30,"fee_type":"taker","slippage_pct":0.05}' | python3 -m json.tool

# ── Register + Login ──
curl -s -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"Test123456!"}' | python3 -m json.tool

curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"Test123456!"}' | python3 -m json.tool
```

### 5. WebSocket test

```bash
# Instalar wscat
npm install -g wscat

# Conectar (sin auth = anonymous tier)
wscat -c "ws://localhost:8000/ws/funding"

# Una vez conectado, enviar:
{"action":"subscribe","channels":["funding","arbitrage"]}
```

### 6. Verificar DB

```bash
docker compose exec postgres psql -U funding_user -d funding_radar -c \
  "SELECT COUNT(*) as rows FROM funding_rates;"

docker compose exec postgres psql -U funding_user -d funding_radar -c \
  "SELECT e.slug, t.symbol, fr.funding_apr, fr.mark_price, fr.time
   FROM funding_rates fr
   JOIN exchanges e ON e.id = fr.exchange_id
   JOIN tokens t ON t.id = fr.token_id
   ORDER BY fr.time DESC LIMIT 10;"
```

### 7. Unit tests

```bash
docker compose exec app pytest -v
```

---

## Checklist

| # | Test | ⬜ |
|---|------|----|
| 1 | `docker compose up -d --build` arranca sin errores | |
| 2 | `alembic upgrade head` OK | |
| 3 | `seed_exchanges.py` inserta 2 exchanges + 18 tokens | |
| 4 | `/health` → 200 | |
| 5 | `/ready` → 200 | |
| 6 | `/collectors/status` → ambos running | |
| 7 | Redis tiene `funding:ranked` (espera 30-60s) | |
| 8 | Redis tiene `arbitrage:current` | |
| 9 | `GET /api/v1/funding/rates` devuelve datos | |
| 10 | `GET /api/v1/arbitrage/opportunities` devuelve pares | |
| 11 | `POST /api/v1/simulator/calculate` calcula PnL | |
| 12 | Auth register → login → JWT → API key funciona | |
| 13 | WebSocket recibe updates | |
| 14 | `funding_rates` tiene rows en DB tras 3 min | |
| 15 | `bash scripts/test_backend.sh` pasa | |

---

## Troubleshooting

**App no arranca / import error:**
```bash
docker compose logs app | tail -50
```
Busca `ModuleNotFoundError` o `ImportError`. Suele ser un requirements faltante.

**Collectors no reciben datos:**
```bash
# Test directo contra las APIs
curl -s -X POST https://api.hyperliquid.xyz/info -H "Content-Type: application/json" -d '{"type":"metaAndAssetCtxs"}' | python3 -m json.tool | head -20

curl -s https://fapi.asterdex.com/fapi/v1/premiumIndex | python3 -m json.tool | head -20
```
Si devuelven datos, el problema es interno. Si no, es un problema de red/firewall.

**Redis vacío después de 2 minutos:**
```bash
docker compose exec redis redis-cli -a redisdev123 KEYS "*"
```
Si hay keys `funding:latest:*` pero no `funding:ranked`, el `FundingService` no está procesando. Check logs.

**Migration falla "extension timescaledb does not exist":**
```bash
docker compose exec postgres psql -U funding_user -d funding_radar -c "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"
# Luego reintenta: docker compose exec app alembic upgrade head
```

**Alembic falla con "Target database is not up to date":**
```bash
docker compose exec app alembic stamp head
docker compose exec app alembic upgrade head
```
