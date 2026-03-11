<div align="center">

# 🎯 Funding Radar

**DEX funding rate aggregator for perpetual futures arbitrage signals**

Aggregates funding rates across Hyperliquid and Aster, detecting convergence/divergence
arbitrage opportunities in decentralized perpetual futures markets.

![Status](https://img.shields.io/badge/Status-Live-brightgreen?style=flat-square)
![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white)

</div>

---

## Why this exists

Perpetual DEXs (like Hyperliquid and Aster) often have vastly different funding rates for the same asset.
As a heavy user of these platforms, I needed a way to visualize funding rate trends historically to
identify mean-reversion and basis trading opportunities. Existing tools didn't cover the long-tail DEXs I traded on.
So I built it myself.

---

## How it works

1. Continually polls rates from Hyperliquid and Aster API endpoints.
2. Normalizes the data structures across exchanges.
3. Ingests into TimescaleDB optimized for time-series operations.
4. Serves data via FastAPI to a dashboard, flagging extreme divergences.

---

## Run it

```bash
git clone https://github.com/Kowalskk/funding-radar.git
cd funding-radar
cp .env.example .env
docker-compose up -d
```

---

## What I learned building this

- **Time-series DBs change the game.** Writing to standard Postgres was fine at first, but switching to TimescaleDB (hypertable) drastically improved query performance on historical rollups.
- **DEX APIs fail silently.** Building robust retry logic and exponential backoffs was critical to maintain data integrity when scraping long-tail decentralised exchange endpoints.

---

## License

MIT
