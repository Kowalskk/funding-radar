# 🚀 FundingRadar — Clon de Smartbitrage
## Guía Completa de Arquitectura + Prompts para Backend

---

## 📋 Resumen del Proyecto

**Nombre propuesto:** FundingRadar (o el que prefieras)

**Qué hace Smartbitrage (y lo que vamos a replicar):**
- Agrega funding rates de perpetuos de 16+ DEXs en tiempo real
- Compara precios (spreads) entre exchanges para cada token
- Calcula APR de oportunidades de arbitraje delta-neutral (long en un DEX, short en otro)
- Muestra open interest, volumen 24h por exchange
- Simulador de posiciones (PnL estimado según días y capital)
- Historial de funding rates con gráficos
- Notificaciones por Telegram (Pro)
- Pricing: Free (1 min updates, 3 días histórico) / Pro $17/mo (10s updates, 31 días, filtros, notificaciones)

---

## 🏗️ Arquitectura del Sistema

### Visión General

```
┌─────────────────────────────────────────────────────────┐
│                    FRONTEND (Chechu)                     │
│              Next.js / React + TailwindCSS               │
│         WebSocket client ← → REST API client             │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP + WebSocket
┌────────────────────▼────────────────────────────────────┐
│                  API GATEWAY (FastAPI)                    │
│  REST endpoints + WebSocket server + Auth/Rate limiting   │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│              BACKEND ENGINE (Python)                      │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Data Fetcher │  │  Processor   │  │  Calculator   │  │
│  │  (Collectors) │  │  (Normalizer)│  │  (Arbitrage)  │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬────────┘  │
│         │                 │                  │           │
│  ┌──────▼─────────────────▼──────────────────▼────────┐  │
│  │              Unified Data Store (Redis)              │  │
│  └─────────────────────────┬──────────────────────────┘  │
│                            │                             │
│  ┌─────────────────────────▼──────────────────────────┐  │
│  │         PostgreSQL (histórico + usuarios)            │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │         Notificaciones (Telegram Bot)               │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### Stack Tecnológico (Backend)

| Componente | Tecnología | Razón |
|-----------|-----------|-------|
| Framework API | **FastAPI** | Async nativo, WebSocket built-in, auto-docs |
| Data Collection | **asyncio + aiohttp + websockets** | Múltiples conexiones simultáneas |
| Cache en memoria | **Redis** | Pub/sub para WebSocket broadcast, cache rápido |
| Base de datos | **PostgreSQL + TimescaleDB** | Series temporales de funding rates |
| Task scheduling | **APScheduler** o **Celery Beat** | Jobs periódicos de fetching |
| Notificaciones | **python-telegram-bot** | Alertas customizables |
| Deployment | **Docker Compose** | Fácil de desplegar en VPS |

---

## 📡 APIs de los DEXs (Directas vs. Agregador)

### ¿Directas o agregador?

**Recomendación: APIs directas de cada DEX.** Razones:

1. **Sin costes** — Las APIs de Hyperliquid, Aster, etc. son gratuitas y públicas
2. **Sin intermediarios** — No dependes de uptime/pricing de terceros
3. **Datos más frescos** — WebSocket directo = latencia mínima
4. **Control total** — Puedes elegir exactamente qué datos traer
5. **Escalable** — Añadir un DEX nuevo = crear un nuevo collector module

Los agregadores como CCXT o Coinalyze son útiles para CEXs pero los DEXs de perps on-chain tienen sus propias APIs muy bien documentadas.

### Detalle de APIs por Exchange

#### 1. Hyperliquid (Prioridad 1)

**Base URL:** `https://api.hyperliquid.xyz`
**WebSocket:** `wss://api.hyperliquid.xyz/ws`
**SDK Python:** `pip install hyperliquid-python-sdk`
**Auth:** No necesaria para datos públicos
**Rate limits:** 1200 req/min por IP, max 10 WS connections, 1000 WS subscriptions

**Endpoints clave:**

```
POST /info
├── type: "metaAndAssetCtxs"     → Metadata + funding actual + mark price + OI + volumen
├── type: "fundingHistory"       → Histórico de funding por coin
│   params: { coin: "BTC", startTime: <ms> }
├── type: "predictedFundings"    → Funding rates predichos
└── type: "allMids"              → Precios mid de todos los assets

WebSocket subscriptions:
├── type: "allMids"              → Streaming de precios mid (cada bloque)
├── type: "l2Book"               → Order book updates
└── type: "trades"               → Trades en tiempo real
```

**Datos que obtenemos por asset:**
- `funding` — Funding rate actual (1h rate, multiplicar x8 para 8h)
- `markPx` — Mark price
- `oraclePx` — Oracle/spot price
- `openInterest` — Open Interest
- `dayNtlVlm` — Volumen 24h notional
- `premium` — Premium index
- `impactPxs` — [impact_bid, impact_ask]

**Fórmula Funding → APR:**
```
hourly_rate = funding_rate (ya viene como 1h rate)
apr = hourly_rate * 24 * 365 * 100
```

#### 2. Aster (Prioridad 1)

**Base URL:** `https://fapi.asterdex.com`
**WebSocket:** `wss://fstream.asterdex.com`
**Auth:** No necesaria para datos públicos
**Estilo API:** Binance-compatible (misma estructura)
**Rate limits:** Weight-based, similar a Binance Futures

**Endpoints clave:**

```
GET /fapi/v1/premiumIndex             → Funding rate actual por symbol
GET /fapi/v1/fundingRate              → Histórico de funding
    params: symbol, startTime, endTime, limit
GET /fapi/v1/ticker/24hr              → Volumen 24h, OI, prices
GET /fapi/v1/openInterest             → Open Interest por symbol
GET /fapi/v1/exchangeInfo             → Todos los pares disponibles

WebSocket streams (wss://fstream.asterdex.com):
├── <symbol>@markPrice               → Mark price + funding cada 3s
├── <symbol>@markPrice@1s            → Mark price + funding cada 1s
├── !markPrice@arr                   → Todos los mark prices
├── <symbol>@ticker                  → 24h ticker stats
└── !miniTicker@arr                  → Mini tickers de todos
```

**Nota:** Los symbols son lowercase en WS (ej: `btcusdt@markPrice`)

**Datos del premiumIndex:**
- `lastFundingRate` — Último funding rate aplicado
- `markPrice` — Mark price
- `indexPrice` — Index/spot price
- `nextFundingTime` — Timestamp próximo funding
- `interestRate` — Interest rate component

**Fórmula Funding → APR:**
```
# Aster paga cada 8h
eight_hour_rate = lastFundingRate
apr = eight_hour_rate * 3 * 365 * 100
```

#### 3. Exchanges Futuros (para ir añadiendo)

| Exchange | API Style | Funding Interval | Docs |
|---------|----------|-----------------|------|
| **Backpack** | Custom REST+WS | 1h | docs.backpack.exchange |
| **Paradex** | Custom REST+WS | 1h | docs.paradex.trade |
| **Lighter** | Custom REST | 8h | docs.lighter.xyz |
| **Variational** | Custom REST | Variable | docs.variational.io |
| **Pacifica** | Custom REST | 8h | docs.pacifica.fi |
| **Ethereal** | Custom REST+WS | 8h | docs.ethereal.trade |

---

## 📂 Estructura de Archivos del Backend

```
funding-radar/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── alembic/                          # DB migrations
│   ├── versions/
│   └── env.py
├── app/
│   ├── __init__.py
│   ├── main.py                       # FastAPI app entry point
│   ├── config.py                     # Settings, env vars
│   ├── dependencies.py               # Dependency injection
│   │
│   ├── api/                          # REST + WebSocket endpoints
│   │   ├── __init__.py
│   │   ├── v1/
│   │   │   ├── __init__.py
│   │   │   ├── router.py             # Main router
│   │   │   ├── funding.py            # GET /funding/rates, /funding/history
│   │   │   ├── arbitrage.py          # GET /arbitrage/opportunities
│   │   │   ├── exchanges.py          # GET /exchanges, /exchanges/{id}/tokens
│   │   │   └── simulator.py          # POST /simulator/calculate
│   │   └── ws/
│   │       ├── __init__.py
│   │       └── funding_stream.py     # WebSocket /ws/funding
│   │
│   ├── collectors/                   # Data fetchers (1 per DEX)
│   │   ├── __init__.py
│   │   ├── base.py                   # BaseCollector abstract class
│   │   ├── hyperliquid.py            # Hyperliquid collector
│   │   ├── aster.py                  # Aster collector
│   │   └── registry.py              # Dynamic collector registry
│   │
│   ├── processors/                   # Data normalization + calculation
│   │   ├── __init__.py
│   │   ├── normalizer.py            # Normalize all DEX data to unified format
│   │   ├── arbitrage_calculator.py   # Calculate cross-DEX arbitrage opportunities
│   │   ├── funding_aggregator.py     # Aggregate + rank funding rates
│   │   └── apr_calculator.py         # Convert rates to APR
│   │
│   ├── models/                       # SQLAlchemy + Pydantic models
│   │   ├── __init__.py
│   │   ├── db/
│   │   │   ├── funding_rate.py       # TimescaleDB hypertable
│   │   │   ├── exchange.py
│   │   │   ├── token.py
│   │   │   └── user.py
│   │   └── schemas/
│   │       ├── funding.py            # API response schemas
│   │       ├── arbitrage.py
│   │       └── simulator.py
│   │
│   ├── services/                     # Business logic layer
│   │   ├── __init__.py
│   │   ├── funding_service.py        # Orchestrates collectors + processors
│   │   ├── arbitrage_service.py      # Finds and ranks opportunities
│   │   ├── notification_service.py   # Telegram alerts
│   │   └── cache_service.py          # Redis operations
│   │
│   ├── core/                         # Infrastructure
│   │   ├── __init__.py
│   │   ├── database.py               # PostgreSQL connection
│   │   ├── redis.py                  # Redis connection
│   │   ├── scheduler.py              # APScheduler setup
│   │   └── websocket_manager.py      # WS connection manager
│   │
│   └── utils/
│       ├── __init__.py
│       ├── rate_limiter.py
│       └── helpers.py
│
└── tests/
    ├── test_collectors/
    ├── test_processors/
    └── test_api/
```

---

## 📊 Modelo de Datos Unificado

### Formato interno normalizado (lo que circula por el sistema)

```python
@dataclass
class NormalizedFundingData:
    """Formato unificado para datos de cualquier DEX"""
    exchange: str               # "hyperliquid", "aster"
    token: str                  # "BTC", "ETH" — normalizado sin suffixes
    symbol: str                 # Symbol original del exchange ("BTCUSDT", "BTC")
    
    # Funding
    funding_rate: float         # Rate crudo del exchange
    funding_rate_8h: float      # Normalizado a 8h para comparación
    funding_apr: float          # APR anualizado
    funding_interval_hours: int # 1 para Hyperliquid, 8 para Aster
    next_funding_time: int      # Timestamp ms
    predicted_rate: float | None
    
    # Prices
    mark_price: float
    index_price: float          # Oracle/spot price
    
    # Market data
    open_interest_usd: float
    volume_24h_usd: float
    
    # Spread
    price_spread_pct: float     # (mark - index) / index * 100
    
    # Fees
    maker_fee: float
    taker_fee: float
    
    # Meta
    timestamp: int              # Unix ms
    is_live: bool               # WebSocket vs REST polling
```

### Formato de oportunidad de arbitraje

```python
@dataclass
class ArbitrageOpportunity:
    token: str
    
    # Long side (buy/long en el exchange con funding negativo o menor)
    long_exchange: str
    long_funding_8h: float
    long_funding_apr: float
    long_mark_price: float
    long_maker_fee: float
    long_taker_fee: float
    
    # Short side
    short_exchange: str
    short_funding_8h: float
    short_funding_apr: float
    short_mark_price: float
    short_maker_fee: float
    short_taker_fee: float
    
    # Calculated
    funding_delta_apr: float    # Diferencia de APR (lo que ganas)
    price_spread_pct: float     # Spread de precio entre exchanges
    net_apr_maker: float        # APR neto descontando fees maker
    net_apr_taker: float        # APR neto descontando fees taker
    
    # Filtering data
    min_open_interest: float    # OI del exchange con menos liquidez
    min_volume_24h: float
    
    timestamp: int
```

---

## ⚡ Flujo de Datos en Tiempo Real

```
1. COLLECTION (cada collector en su propio asyncio task)
   │
   ├── HyperliquidCollector
   │   ├── WebSocket "allMids" → precios mid cada bloque (~1s)
   │   ├── REST "metaAndAssetCtxs" polling cada 10s → funding + OI + vol
   │   └── REST "fundingHistory" cada 1h → guardar histórico
   │
   └── AsterCollector
       ├── WebSocket "!markPrice@arr" → mark + funding cada 3s
       ├── REST "/fapi/v1/ticker/24hr" polling cada 30s → vol + OI
       └── REST "/fapi/v1/fundingRate" cada 1h → guardar histórico
   │
   ▼
2. NORMALIZATION
   │ Cada collector emite NormalizedFundingData
   │ Se publica en Redis channel "funding:updates"
   │
   ▼
3. PROCESSING (subscribers del Redis channel)
   │
   ├── ArbitrageCalculator
   │   └── Para cada token presente en 2+ exchanges:
   │       - Calcula funding delta
   │       - Calcula price spread
   │       - Calcula net APR (descontando fees)
   │       - Publica en Redis "arbitrage:opportunities"
   │
   ├── FundingAggregator
   │   └── Agrupa por token, ordena por APR
   │       - Publica en Redis "funding:ranked"
   │
   └── HistoryWriter
       └── Guarda snapshots en PostgreSQL/TimescaleDB
   │
   ▼
4. DELIVERY
   │
   ├── WebSocket Server
   │   └── Subscribe a Redis channels → broadcast a clientes conectados
   │
   ├── REST API
   │   └── Lee de Redis cache → responde queries
   │
   └── Telegram Notifier
       └── Chequea umbrales de APR → envía alertas
```

---

## 🗄️ Schema de Base de Datos (PostgreSQL + TimescaleDB)

```sql
-- Exchanges soportados
CREATE TABLE exchanges (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(50) UNIQUE NOT NULL,      -- "hyperliquid", "aster"
    name VARCHAR(100) NOT NULL,             -- "Hyperliquid", "Aster"
    logo_url VARCHAR(500),
    maker_fee DECIMAL(10,6) DEFAULT 0,
    taker_fee DECIMAL(10,6) DEFAULT 0,
    funding_interval_hours INT DEFAULT 8,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Tokens/markets disponibles
CREATE TABLE tokens (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,            -- "BTC", "ETH"
    name VARCHAR(100),
    is_active BOOLEAN DEFAULT true,
    UNIQUE(symbol)
);

-- Exchange-specific token mappings
CREATE TABLE exchange_tokens (
    id SERIAL PRIMARY KEY,
    exchange_id INT REFERENCES exchanges(id),
    token_id INT REFERENCES tokens(id),
    exchange_symbol VARCHAR(50) NOT NULL,    -- "BTCUSDT" para Aster, "BTC" para HL
    max_leverage INT,
    is_active BOOLEAN DEFAULT true,
    UNIQUE(exchange_id, token_id)
);

-- Funding rate history (TimescaleDB hypertable)
CREATE TABLE funding_rates (
    time TIMESTAMPTZ NOT NULL,
    exchange_id INT NOT NULL REFERENCES exchanges(id),
    token_id INT NOT NULL REFERENCES tokens(id),
    funding_rate DECIMAL(20,12),
    funding_rate_8h DECIMAL(20,12),
    funding_apr DECIMAL(20,6),
    mark_price DECIMAL(30,10),
    index_price DECIMAL(30,10),
    open_interest_usd DECIMAL(30,2),
    volume_24h_usd DECIMAL(30,2),
    price_spread_pct DECIMAL(10,6),
    PRIMARY KEY (time, exchange_id, token_id)
);

-- Convertir a hypertable de TimescaleDB
SELECT create_hypertable('funding_rates', 'time');

-- Índices para queries rápidas
CREATE INDEX idx_funding_rates_token ON funding_rates (token_id, time DESC);
CREATE INDEX idx_funding_rates_exchange ON funding_rates (exchange_id, time DESC);

-- Compression policy (datos > 7 días se comprimen)
ALTER TABLE funding_rates SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange_id, token_id'
);
SELECT add_compression_policy('funding_rates', INTERVAL '7 days');

-- Retention policy (borrar datos > 90 días para free, mantener todo para pro)
-- Se gestiona desde el backend según tier del usuario

-- Usuarios (para monetización)
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE,
    telegram_chat_id BIGINT UNIQUE,
    tier VARCHAR(20) DEFAULT 'free',        -- 'free', 'pro', 'custom'
    api_key VARCHAR(64) UNIQUE,
    stripe_customer_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Notificación configs
CREATE TABLE notification_rules (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    token_symbol VARCHAR(20),               -- NULL = todos los tokens
    min_apr DECIMAL(10,2),                   -- Mínimo APR para notificar
    exchanges TEXT[],                        -- Exchanges a monitorear
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 🔐 Autenticación y Tiers

### Tier Free
- Polling REST cada 60 segundos (no WebSocket)
- Historial de 3 días
- Sin filtros avanzados
- Sin notificaciones

### Tier Pro ($17/mo)
- WebSocket en tiempo real (10s updates)
- Historial de 31 días
- Filtros personalizados (exchanges, tokens)
- Notificaciones Telegram
- Pinned exchanges

### Implementación
- **API Key** en header `X-API-Key` para REST
- **JWT token** via query param `?token=xxx` para WebSocket
- **Rate limiting** por tier en Redis (token bucket)
- **Stripe** para pagos recurrentes

---

## 🔧 Configuración (.env)

```env
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/fundingradar
REDIS_URL=redis://localhost:6379/0

# API
API_HOST=0.0.0.0
API_PORT=8000
SECRET_KEY=your-secret-key
JWT_ALGORITHM=HS256

# Exchange configs
HYPERLIQUID_API_URL=https://api.hyperliquid.xyz
HYPERLIQUID_WS_URL=wss://api.hyperliquid.xyz/ws
ASTER_API_URL=https://fapi.asterdex.com
ASTER_WS_URL=wss://fstream.asterdex.com

# Telegram
TELEGRAM_BOT_TOKEN=your-bot-token

# Stripe
STRIPE_SECRET_KEY=sk_live_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx

# Collector settings
POLLING_INTERVAL_SECONDS=10
HISTORY_SAVE_INTERVAL_SECONDS=3600
```

---

## 🎯 Prompts para Claude Sonnet (Backend Development)

### Cómo usar estos prompts

Cada prompt está diseñado para una sesión de Claude Sonnet. Sigue el orden numérico. Cada sesión produce archivos funcionales que se integran con los anteriores. Copia el prompt completo incluyendo contexto.

---

### PROMPT 1: Proyecto Base + Configuración + Docker

```
Necesito que me crees la estructura base de un proyecto Python llamado "funding-radar". Es un backend que agrega funding rates de DEXs de perpetuos en tiempo real (como Smartbitrage.com).

Stack:
- FastAPI (async)
- PostgreSQL + TimescaleDB (funding rate history)
- Redis (cache + pub/sub)
- Docker Compose

Crea los siguientes archivos:

1. `docker-compose.yml` — servicios: app, postgres (con TimescaleDB), redis
2. `Dockerfile` — Python 3.12, instalación de dependencias
3. `requirements.txt` — fastapi, uvicorn, asyncpg, sqlalchemy[asyncio], alembic, redis[hiredis], aiohttp, websockets, python-telegram-bot, apscheduler, pydantic-settings, stripe, python-jose[cryptography], passlib[bcrypt]
4. `.env.example` — todas las variables necesarias
5. `app/config.py` — Pydantic Settings class que lee de .env
6. `app/main.py` — FastAPI app con startup/shutdown events que:
   - Inicializa pool de PostgreSQL (async)
   - Conecta a Redis
   - Registra routers de API v1
   - Monta WebSocket endpoint
   - Inicia scheduler para jobs periódicos
7. `app/core/database.py` — SQLAlchemy async engine + session
8. `app/core/redis.py` — Redis async connection pool
9. `app/core/scheduler.py` — APScheduler async setup

Incluye los archivos __init__.py necesarios. Asegúrate de que el docker-compose expone el puerto 8000 para la API. El código debe ser production-ready con logging, error handling, y graceful shutdown.
```

---

### PROMPT 2: Modelos de Base de Datos + Migrations

```
Continúo el proyecto "funding-radar" (backend FastAPI para agregar funding rates de DEXs de perpetuos).

Necesito que crees los modelos de base de datos y las migrations con Alembic.

Contexto del schema:
- exchanges: slug, name, logo_url, maker_fee, taker_fee, funding_interval_hours, is_active
- tokens: symbol, name, is_active
- exchange_tokens: mapeo exchange↔token con exchange_symbol y max_leverage
- funding_rates: TimescaleDB hypertable con time, exchange_id, token_id, funding_rate, funding_rate_8h, funding_apr, mark_price, index_price, open_interest_usd, volume_24h_usd, price_spread_pct
- users: email, telegram_chat_id, tier (free/pro/custom), api_key, stripe_customer_id
- notification_rules: user_id, token_symbol, min_apr, exchanges (array), is_active

Crea:
1. `app/models/db/exchange.py` — SQLAlchemy model Exchange
2. `app/models/db/token.py` — SQLAlchemy model Token + ExchangeToken
3. `app/models/db/funding_rate.py` — SQLAlchemy model FundingRate
4. `app/models/db/user.py` — SQLAlchemy model User + NotificationRule
5. `app/models/db/__init__.py` — exporta todo
6. `app/models/schemas/funding.py` — Pydantic schemas para API responses (FundingRateResponse, FundingHistoryResponse, etc.)
7. `app/models/schemas/arbitrage.py` — Pydantic schemas (ArbitrageOpportunity, ArbitrageListResponse)
8. `app/models/schemas/simulator.py` — Pydantic schemas (SimulatorRequest, SimulatorResponse)
9. `alembic/env.py` — configurado para async
10. Migration inicial que:
    - Crea todas las tablas
    - Activa TimescaleDB extension
    - Convierte funding_rates a hypertable
    - Crea índices
    - Seed data: inserta Hyperliquid y Aster como exchanges con sus fees reales
      - Hyperliquid: maker 0.01%, taker 0.035%, funding cada 1h
      - Aster: maker 0.01%, taker 0.035%, funding cada 8h

Usa SQLAlchemy 2.0 style con mapped_column. Los modelos deben usar async sessions.
```

---

### PROMPT 3: Base Collector + Hyperliquid Collector

```
Continúo el proyecto "funding-radar". Ahora necesito el sistema de collectors que obtienen datos de los DEXs.

Primero, crea la clase base abstracta y luego el collector de Hyperliquid.

Formato normalizado que todos los collectors deben emitir:
```python
@dataclass
class NormalizedFundingData:
    exchange: str               # "hyperliquid"
    token: str                  # "BTC" — normalizado
    symbol: str                 # Symbol original del exchange
    funding_rate: float         # Rate crudo
    funding_rate_8h: float      # Normalizado a 8h
    funding_apr: float          # Anualizado
    funding_interval_hours: int
    next_funding_time: int | None
    predicted_rate: float | None
    mark_price: float
    index_price: float
    open_interest_usd: float
    volume_24h_usd: float
    price_spread_pct: float
    maker_fee: float
    taker_fee: float
    timestamp: int
    is_live: bool
```

Crea:

1. `app/collectors/base.py` — BaseCollector ABC con:
   - `__init__(self, redis_client, config)` 
   - `async start()` — inicia collection (WS + polling)
   - `async stop()` — graceful shutdown
   - `async _publish(data: NormalizedFundingData)` — publica en Redis channel "funding:updates"
   - `async _fetch_rest(url, method, payload)` — helper HTTP con retry y rate limiting
   - `_normalize(raw_data) -> list[NormalizedFundingData]` — abstract
   - Logging integrado, reconnection automática para WebSocket

2. `app/collectors/hyperliquid.py` — HyperliquidCollector que:
   - WebSocket: conecta a wss://api.hyperliquid.xyz/ws, subscribe a "allMids" para precios en tiempo real
   - REST polling cada 10s: POST /info con type "metaAndAssetCtxs" para funding rates + OI + volume
   - REST cada 1h: POST /info con type "fundingHistory" para guardar histórico
   - Normalización:
     - funding_rate viene como rate horario → funding_rate_8h = rate * 8
     - funding_apr = rate * 24 * 365 * 100
     - open_interest = raw OI * mark_price (viene en unidades del asset)
     - volume_24h = dayNtlVlm (ya en USD)
     - token = asset name directo ("BTC", "ETH")
   - Manejo de reconexión WS con exponential backoff
   - Filtering: excluir assets con OI < $1000 o sin volumen

3. `app/collectors/registry.py` — CollectorRegistry:
   - Registro dinámico de collectors
   - `register(name, collector_class)`
   - `start_all()` — inicia todos los collectors como tasks async
   - `stop_all()` — graceful shutdown

La API de Hyperliquid no necesita auth. Base URL: https://api.hyperliquid.xyz
WS URL: wss://api.hyperliquid.xyz/ws
Rate limits: 1200 req/min, max 10 WS connections.

El código debe ser robusto con retry logic, proper error handling, y no debe crashear si un exchange está caído.
```

---

### PROMPT 4: Aster Collector

```
Continúo el proyecto "funding-radar". Ya tengo el BaseCollector y el HyperliquidCollector. Ahora necesito el AsterCollector.

Aster tiene una API estilo Binance Futures. Los detalles:

Base URL: https://fapi.asterdex.com
WebSocket: wss://fstream.asterdex.com

Endpoints REST:
- GET /fapi/v1/premiumIndex → funding actual de todos los symbols
  Response: [{ symbol, markPrice, indexPrice, lastFundingRate, nextFundingTime, interestRate }]
- GET /fapi/v1/ticker/24hr → volume, OI, precios
  Response: [{ symbol, volume, quoteVolume, openInterest, ... }]
- GET /fapi/v1/fundingRate?symbol=BTCUSDT&limit=100 → histórico
- GET /fapi/v1/exchangeInfo → info de todos los pares

WebSocket streams (todo lowercase):
- wss://fstream.asterdex.com/stream?streams=!markPrice@arr → todos los mark prices + funding cada 3s
- wss://fstream.asterdex.com/stream?streams=btcusdt@markPrice@1s → por symbol cada 1s

Formato WebSocket markPrice:
{
  "e": "markPriceUpdate",
  "E": 1562305380000,    // event time
  "s": "BTCUSDT",        // symbol
  "p": "11794.15000000", // mark price
  "i": "11784.62659091", // index price
  "P": "11784.25641265", // estimated settle price
  "r": "0.00038167",     // funding rate
  "T": 1562306400000     // next funding time
}

Crea `app/collectors/aster.py` — AsterCollector que:
- Hereda de BaseCollector
- WebSocket: conecta al combined stream !markPrice@arr para todos los tokens
- REST polling cada 30s: /fapi/v1/ticker/24hr para OI y volumen
- REST cada 1h: /fapi/v1/fundingRate para histórico
- Normalización:
  - Symbol: "BTCUSDT" → token "BTC" (strip "USDT" suffix)
  - funding rate de Aster es 8h → funding_rate_8h = lastFundingRate
  - funding_apr = lastFundingRate * 3 * 365 * 100
  - Fees: maker 0.01%, taker 0.035%
- Manejo especial: Aster WS usa el formato de streams combinados
- Rate limit handling: respetar header X-MBX-USED-WEIGHT

Asegúrate de que la normalización produce el mismo NormalizedFundingData que Hyperliquid para que sean directamente comparables.
```

---

### PROMPT 5: Procesadores (Arbitrage Calculator + Aggregator)

```
Continúo el proyecto "funding-radar". Ya tengo los collectors de Hyperliquid y Aster publicando NormalizedFundingData en Redis channel "funding:updates".

Ahora necesito los procesadores que consumen esos datos y calculan oportunidades de arbitraje.

Crea:

1. `app/processors/normalizer.py` — DataNormalizer:
   - Mantiene un dict en memoria con el último dato de cada (exchange, token)
   - Recibe updates del Redis channel "funding:updates"
   - Agrupa datos por token para tener vista cross-exchange

2. `app/processors/arbitrage_calculator.py` — ArbitrageCalculator:
   - Para cada token que existe en 2+ exchanges:
     - Encuentra el par con mayor diferencia de funding (uno positivo alto, otro negativo o bajo)
     - Calcula: long en el exchange con funding más negativo (cobras), short en el con funding más positivo (pagas menos)
     - funding_delta_apr = abs(short_funding_apr - long_funding_apr)
     - price_spread_pct = abs(mark_price_a - mark_price_b) / avg(mark_price_a, mark_price_b) * 100
     - net_apr_maker = funding_delta_apr - (long_maker_fee + short_maker_fee) * 3 * 365 * 100
     - net_apr_taker = funding_delta_apr - (long_taker_fee + short_taker_fee) * 3 * 365 * 100
   - Ordena por net_apr_taker descendente
   - Publica resultado en Redis key "arbitrage:current" (JSON)
   - También publica en Redis channel "arbitrage:updates" para WebSocket

3. `app/processors/funding_aggregator.py` — FundingAggregator:
   - Agrupa funding data por token
   - Para cada token, lista los funding rates de todos los exchanges
   - Ordena tokens por max APR (el más alto de cualquier exchange)
   - Publica en Redis key "funding:ranked" (JSON)
   - También publica en Redis channel "funding:ranked:updates"

4. `app/processors/apr_calculator.py` — utilidades:
   - `funding_to_8h(rate, interval_hours)` — normaliza cualquier rate a 8h
   - `funding_to_apr(rate_8h)` — rate 8h a APR anual
   - `calculate_pnl(apr, capital, days)` — simula PnL

5. `app/services/funding_service.py` — FundingService:
   - Orquesta: inicia un subscriber al Redis channel
   - Por cada update: actualiza normalizer → recalcula arbitrage → recalcula rankings
   - Guarda snapshots periódicos en PostgreSQL

Todo debe ser async y manejar el caso de que un exchange esté caído (usar últimos datos válidos con timestamp).
```

---

### PROMPT 6: API REST Endpoints

```
Continúo el proyecto "funding-radar". Ya tengo collectors, procesadores y datos en Redis. Ahora necesito los endpoints REST de la API.

Crea:

1. `app/api/v1/funding.py`:
   - GET /api/v1/funding/rates
     - Query params: timeframe (live, 1h, 8h, 24h, 3d, 7d, 15d, 31d), exchanges[], token
     - Response: lista de tokens con funding rates por exchange, ordenados por APR
     - Lee de Redis "funding:ranked"
   
   - GET /api/v1/funding/history/{token}
     - Query params: exchange, timeframe (24h, 3d, 7d, 15d, 31d), interval
     - Response: serie temporal de funding rates
     - Lee de PostgreSQL

   - GET /api/v1/funding/token/{token}
     - Response: detalle completo de un token en todos los exchanges (como la vista de detalle de Smartbitrage — funding history chart, arbitrage summary, live snapshot)

2. `app/api/v1/arbitrage.py`:
   - GET /api/v1/arbitrage/opportunities
     - Query params: min_apr, min_oi, exchanges[], limit
     - Response: lista de ArbitrageOpportunity ordenadas por net_apr
     - Lee de Redis "arbitrage:current"

3. `app/api/v1/simulator.py`:
   - POST /api/v1/simulator/calculate
     - Body: { token, long_exchange, short_exchange, capital_usd, days, fee_type: "maker"|"taker", slippage_pct }
     - Response: { estimated_pnl, estimated_apr, daily_funding_income, total_fees, net_profit }

4. `app/api/v1/exchanges.py`:
   - GET /api/v1/exchanges — lista de exchanges activos con stats
   - GET /api/v1/exchanges/{slug}/tokens — tokens disponibles en un exchange

5. `app/api/v1/router.py` — main router que monta todos los sub-routers

Cada endpoint debe:
- Validar parámetros con Pydantic
- Implementar rate limiting por tier (free: 60 req/min, pro: 600 req/min)
- Devolver respuestas paginadas donde sea necesario
- Manejar errores con HTTPException apropiados
- Incluir response_model para auto-documentación OpenAPI
```

---

### PROMPT 7: WebSocket Server

```
Continúo el proyecto "funding-radar". Necesito el servidor WebSocket para enviar updates en tiempo real a los clientes del frontend.

Crea:

1. `app/core/websocket_manager.py` — WebSocketManager:
   - Mantiene un dict de conexiones activas agrupadas por channel
   - Channels: "funding", "arbitrage", "funding:{token}" (por token específico)
   - `connect(ws, channel, user_tier)` — registra conexión
   - `disconnect(ws)` — limpia conexión
   - `broadcast(channel, data)` — envía a todos los suscritos al channel
   - Rate limiting: free users reciben updates cada 60s (buffer), pro cada 10s
   - Heartbeat cada 30s para detectar conexiones muertas

2. `app/api/ws/funding_stream.py`:
   - WebSocket endpoint /ws/funding
   - Autenticación via query param ?token=xxx (JWT)
   - Mensaje de subscripción del cliente: { "action": "subscribe", "channels": ["funding", "arbitrage", "funding:BTC"] }
   - Mensaje de unsubscribe: { "action": "unsubscribe", "channels": ["funding:BTC"] }
   - El server escucha Redis pub/sub channels y retransmite a los clientes suscritos
   - Formato de mensaje enviado al frontend:
     {
       "type": "funding_update",
       "data": { ... NormalizedFundingData ... },
       "timestamp": 1234567890
     }
     {
       "type": "arbitrage_update", 
       "data": [{ ... ArbitrageOpportunity ... }],
       "timestamp": 1234567890
     }

3. Integrar el WebSocketManager en `app/main.py`:
   - Crear instancia global
   - Background task que subscribe a Redis channels y llama a broadcast
   - Graceful shutdown de todas las conexiones

El WebSocket debe manejar reconnections del lado del servidor (a Redis) y del lado del cliente (heartbeat + close events).
```

---

### PROMPT 8: Notificaciones Telegram

```
Continúo el proyecto "funding-radar". Necesito el sistema de notificaciones por Telegram.

Crea:

1. `app/services/notification_service.py` — NotificationService:
   - Chequea notification_rules de usuarios Pro contra datos actuales
   - Cuando un arbitrage opportunity supera el min_apr de una regla → envía alerta
   - Throttling: máximo 1 notificación por token por usuario cada 30 minutos
   - Formato del mensaje Telegram:
     ```
     🔔 Arbitrage Alert: {TOKEN}
     
     📈 APR: {net_apr}%
     
     Long: {long_exchange} (funding: {long_apr}%)
     Short: {short_exchange} (funding: {short_apr}%)
     
     💰 Spread: {price_spread}%
     📊 Min OI: ${min_oi}
     
     ⏰ {timestamp}
     ```

2. Bot commands (registrados en la API o como webhook):
   - /start — registro del usuario con chat_id
   - /alerts — ver alertas activas
   - /setalert {token} {min_apr} — crear regla rápida
   - /removealert {id} — eliminar regla
   - /status — estado de exchanges y última actualización

3. Integrar el chequeo de notificaciones en el flujo de procesamiento:
   - Cada vez que se recalculan oportunidades de arbitraje
   - Usar un background task con APScheduler que corre cada 30s

El bot debe ser resiliente a errores de la API de Telegram y no bloquear el flujo principal de datos.
```

---

### PROMPT 9: Autenticación + Stripe + Rate Limiting

```
Continúo el proyecto "funding-radar". Necesito el sistema de auth, pagos y rate limiting.

Crea:

1. `app/api/v1/auth.py`:
   - POST /api/v1/auth/register — { email, password }
   - POST /api/v1/auth/login — devuelve JWT
   - GET /api/v1/auth/me — info del usuario actual
   - POST /api/v1/auth/api-key — genera API key para el usuario

2. `app/services/auth_service.py`:
   - JWT generation/validation
   - API key generation (random 64 char hex)
   - Password hashing con bcrypt

3. `app/services/payment_service.py`:
   - create_checkout_session(user_id) → Stripe Checkout URL
   - handle_webhook(payload) → procesa eventos de Stripe
   - Eventos: checkout.session.completed → upgrade a pro
   - customer.subscription.deleted → downgrade a free

4. `app/api/v1/webhooks.py`:
   - POST /api/v1/webhooks/stripe — webhook de Stripe

5. `app/dependencies.py`:
   - `get_current_user()` — dependency que extrae user de JWT/API key
   - `require_pro()` — dependency que verifica tier pro
   - `rate_limit(tier)` — dependency de rate limiting por tier usando Redis token bucket

6. `app/utils/rate_limiter.py`:
   - Token bucket implementation en Redis
   - Free: 60 req/min, burst 10
   - Pro: 600 req/min, burst 100

Integra las dependencies en los routers existentes.
```

---

### PROMPT 10: Testing + Deployment

```
Continúo el proyecto "funding-radar". Necesito tests y configuración final de deployment.

Crea:

1. Tests:
   - `tests/conftest.py` — fixtures (mock Redis, test DB, test client)
   - `tests/test_collectors/test_hyperliquid.py` — test normalización con datos mock
   - `tests/test_collectors/test_aster.py` — test normalización con datos mock
   - `tests/test_processors/test_arbitrage.py` — test cálculo de oportunidades
   - `tests/test_api/test_funding.py` — test endpoints REST

2. Scripts útiles:
   - `scripts/seed_exchanges.py` — seed data para exchanges (HL, Aster)
   - `scripts/backfill_funding.py` — descarga histórico de funding de ambos exchanges
   - `scripts/health_check.py` — chequeo de salud del sistema

3. Deployment:
   - `nginx.conf` — reverse proxy con SSL, WebSocket upgrade
   - `docker-compose.prod.yml` — producción con volumes, restart policies, health checks
   - `.github/workflows/deploy.yml` — CI/CD básico (lint, test, deploy)

4. `README.md` con:
   - Setup local (docker-compose up)
   - Variables de entorno
   - API documentation link (/docs)
   - Cómo añadir un nuevo exchange
   - Architecture overview

El deployment target es un VPS con Docker (por ejemplo Hetzner o DigitalOcean).
```

---

## 📐 API Contract para el Frontend (para Chechu)

### REST Endpoints Summary

| Method | Endpoint | Descripción | Auth |
|--------|---------|-------------|------|
| GET | /api/v1/funding/rates | Funding rates ranked | No (free tier) |
| GET | /api/v1/funding/history/{token} | Historial por token | No (3d free, 31d pro) |
| GET | /api/v1/funding/token/{token} | Detalle completo de un token | No |
| GET | /api/v1/arbitrage/opportunities | Oportunidades de arb | No |
| POST | /api/v1/simulator/calculate | Simular posición | No |
| GET | /api/v1/exchanges | Lista exchanges | No |
| POST | /api/v1/auth/register | Registro | No |
| POST | /api/v1/auth/login | Login → JWT | No |
| GET | /api/v1/auth/me | Info usuario | JWT |
| POST | /api/v1/auth/api-key | Generar API key | JWT |
| WS | /ws/funding?token=xxx | Stream tiempo real | JWT (pro) |

### WebSocket Messages (Frontend → Backend)

```json
// Subscribe
{ "action": "subscribe", "channels": ["funding", "arbitrage"] }

// Subscribe to specific token
{ "action": "subscribe", "channels": ["funding:BTC", "funding:ETH"] }

// Unsubscribe
{ "action": "unsubscribe", "channels": ["funding:BTC"] }

// Pong (response to server ping)
{ "action": "pong" }
```

### WebSocket Messages (Backend → Frontend)

```json
// Funding update (all tokens)
{
  "type": "funding_update",
  "data": [{
    "token": "BTC",
    "exchanges": {
      "hyperliquid": {
        "funding_rate_8h": 0.0001,
        "funding_apr": 10.95,
        "mark_price": 98500.50,
        "index_price": 98480.00,
        "open_interest_usd": 5000000000,
        "volume_24h_usd": 12000000000,
        "price_spread_pct": 0.02
      },
      "aster": { ... }
    }
  }],
  "timestamp": 1709312345000
}

// Arbitrage opportunities update
{
  "type": "arbitrage_update",
  "data": [{
    "token": "SAHARA",
    "long_exchange": "variational",
    "short_exchange": "aster",
    "funding_delta_apr": 1075.21,
    "price_spread_pct": 0.5,
    "net_apr_taker": 1050.00,
    "min_open_interest": 134010
  }],
  "timestamp": 1709312345000
}

// Heartbeat
{ "type": "ping" }
```

---

## 🗓️ Roadmap Sugerido

### Fase 1 (Semana 1-2): MVP
- [x] Estructura del proyecto + Docker
- [ ] Collectors: Hyperliquid + Aster
- [ ] Procesadores: normalización + ranking
- [ ] API REST básica: /funding/rates, /funding/history
- [ ] Frontend: tabla de funding rates (Chechu)

### Fase 2 (Semana 3-4): Arbitraje
- [ ] ArbitrageCalculator
- [ ] API: /arbitrage/opportunities
- [ ] Simulador de posiciones
- [ ] Frontend: vista de detalle por token + simulador (Chechu)

### Fase 3 (Semana 5-6): Real-time + Auth
- [ ] WebSocket server
- [ ] Auth + JWT + API keys
- [ ] Stripe integration
- [ ] Frontend: upgrades de WebSocket + login (Chechu)

### Fase 4 (Semana 7-8): Notificaciones + Pulido
- [ ] Telegram bot + alertas
- [ ] Añadir 2-3 exchanges más (Backpack, Paradex, Lighter)
- [ ] Landing page + pricing
- [ ] Deploy producción

### Fase 5 (Mes 3+): Escala
- [ ] Añadir los 16 exchanges de Smartbitrage
- [ ] API pública para terceros
- [ ] Tier Custom para institucionales
- [ ] Referral program con descuentos en fees

---

## 💡 Tips para la Implementación

1. **Empieza con REST polling, no WebSocket** — Es más fácil de debuggear. Añade WS después.

2. **Redis es tu mejor amigo** — Usa Redis para TODO el estado en caliente. PostgreSQL solo para histórico.

3. **Un collector por exchange, un formato unificado** — Así añadir un exchange nuevo es copiar-pegar y adaptar normalización.

4. **Los APR se ven enormes (1000%+) en tokens poco líquidos** — Filtra por OI mínimo y volumen. Smartbitrage muestra todo pero los buenos traders saben que APR > 100% en tokens con $50k de OI no vale la pena.

5. **Funding rates cambian cada hora/8h** — No necesitas actualizar cada segundo para la mayoría de usuarios. El "Live" de Smartbitrage es el predicted funding, no el settled.

6. **Para monetizar rápido** — Lanza free tier cuanto antes para captar usuarios, luego añade Pro features.
