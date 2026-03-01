# Funding Radar 📡

> Real-time perpetual funding rate aggregator and cross-exchange arbitrage detector.
> Tracks Hyperliquid, AsterDEX and more — all in one async Python backend.

[![CI/CD](https://github.com/your-org/funding-radar/actions/workflows/deploy.yml/badge.svg)](https://github.com/your-org/funding-radar/actions)

---

## Architecture

```
┌──────────────┐    ┌──────────────┐
│ Hyperliquid  │    │  AsterDEX    │  ← Collectors (WS + REST)
└──────┬───────┘    └──────┬───────┘
       │  NormalizedFundingData     │
       └───────────┬───────────────┘
                   ▼
            Redis pub/sub
          "funding:updates"
                   │
       ┌───────────┴───────────┐
       ▼                       ▼
  DataNormalizer        RedisBridge
  ArbitrageCalc       (WS fan-out)
       │                       │
       ▼                       ▼
  Redis keys            WebSocket clients
  funding:ranked        /ws/funding
  arbitrage:current
       │
       ▼
  REST API (FastAPI)
  /api/v1/funding/*
  /api/v1/arbitrage/*
  /api/v1/simulator/*
  /api/v1/exchanges/*
  /api/v1/auth/*
```

---

## Quick Start (local)

### Prerequisites
- Docker + Docker Compose
- 4 GB RAM

```bash
git clone https://github.com/your-org/funding-radar.git
cd funding-radar

# 1. Copy env and fill in secrets
cp .env.example .env
# Edit .env — only APP_SECRET_KEY, JWT_SECRET_KEY, DATABASE_URL, REDIS_URL are required for local

# 2. Start services
docker compose up -d

# 3. Run migrations
docker compose exec app alembic upgrade head

# 4. Seed exchanges and tokens
docker compose exec app python scripts/seed_exchanges.py

# 5. Open API docs
open http://localhost:8000/docs
```

---

## Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `APP_SECRET_KEY` | 32+ char random secret | `openssl rand -hex 32` |
| `JWT_SECRET_KEY` | JWT signing secret | `openssl rand -hex 32` |
| `DATABASE_URL` | Async PostgreSQL DSN | `postgresql+asyncpg://user:pass@postgres/db` |
| `REDIS_URL` | Redis DSN | `redis://:password@redis:6379/0` |
| `TELEGRAM_BOT_TOKEN` | Optional — alerts bot | From @BotFather |
| `STRIPE_SECRET_KEY` | Optional — payments | `sk_live_...` |
| `STRIPE_WEBHOOK_SECRET` | Optional — webhooks | `whsec_...` |
| `STRIPE_PRICE_ID_PRO` | Pro plan price ID | `price_...` |

See `.env.example` for the full list.

---

## API Documentation

Once running: **[http://localhost:8000/docs](http://localhost:8000/docs)**

### Key endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/v1/funding/rates` | Live funding rates (all or filtered) |
| `GET`  | `/api/v1/funding/history/{token}` | Historical time-series |
| `GET`  | `/api/v1/funding/token/{token}` | Full token detail view |
| `GET`  | `/api/v1/arbitrage/opportunities` | Ranked cross-exchange arb |
| `POST` | `/api/v1/simulator/calculate` | P&L simulation |
| `GET`  | `/api/v1/exchanges` | Active exchange list |
| `POST` | `/api/v1/auth/register` | Create account |
| `POST` | `/api/v1/auth/login` | Get JWT |
| `WS`   | `/ws/funding?token=<jwt>` | Real-time stream |

---

## WebSocket Protocol

```javascript
const ws = new WebSocket('wss://your-domain.com/ws/funding?token=<jwt>');

ws.onopen = () => {
  ws.send(JSON.stringify({
    action: 'subscribe',
    channels: ['funding', 'arbitrage', 'funding:BTC']
  }));
};

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  // msg.type: 'funding_update' | 'arbitrage_update' | 'token_update' | 'ping'
};

// Respond to heartbeat pings
ws.addEventListener('message', (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
});
```

---

## Adding a New Exchange

1. **Create collector**: `app/collectors/{slug}.py`
   - Extend `BaseCollector`
   - Implement `_poll_rest()` → returns `list[NormalizedFundingData]`
   - Implement `_run_ws()` for real-time updates (optional)
   - Set class attrs: `exchange_slug`, `funding_interval_hours`, `maker_fee`, `taker_fee`

2. **Register**: In `app/main.py` lifespan:
   ```python
   _collector_registry.register("my_exchange", MyCollector)
   ```

3. **Seed**: Add exchange + tokens to `scripts/seed_exchanges.py` and run it.

4. **Done** — the normalizer, arbitrage calculator, and API pick it up automatically.

---

## Running Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=app --cov-report=html
open htmlcov/index.html

# One module
pytest tests/test_processors/ -v
```

---

## Production Deployment (VPS)

```bash
# On a fresh Hetzner / DigitalOcean Ubuntu VPS:
# 1. Install Docker, clone repo, copy .env
git clone https://github.com/your-org/funding-radar.git /opt/funding-radar
cd /opt/funding-radar
cp .env.example .env  # fill in production values

# 2. Get TLS cert (certbot)
certbot certonly --standalone -d your-domain.com

# 3. Start production stack
docker compose -f docker-compose.prod.yml up -d

# 4. Run migrations + seed
docker compose -f docker-compose.prod.yml exec app alembic upgrade head
docker compose -f docker-compose.prod.yml exec app python scripts/seed_exchanges.py

# 5. Backfill historical data (optional)
docker compose -f docker-compose.prod.yml exec app \
  python scripts/backfill_funding.py --days 30

# 6. Health check
python scripts/health_check.py --url https://your-domain.com
```

### GitHub Actions secrets required

| Secret | Description |
|--------|-------------|
| `DOCKER_REGISTRY` | e.g. `ghcr.io/your-org` |
| `DOCKER_USERNAME` | Registry login |
| `DOCKER_PASSWORD` | Registry token |
| `VPS_HOST` | VPS IP / hostname |
| `VPS_USER` | SSH user (e.g. `deploy`) |
| `VPS_SSH_KEY` | Private SSH key |

---

## Rate Limits

| Tier | Burst | Sustained |
|------|-------|-----------|
| Anonymous (no key) | 5 req | 5/min |
| Free | 10 req burst | 60/min |
| Pro | 100 req burst | 600/min |
| Custom | 500 req burst | 6000/min |

Upgrade: `POST /api/v1/auth/checkout` → Stripe checkout URL.

---

## License

MIT — see [LICENSE](LICENSE).
