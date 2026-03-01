#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  test_backend.sh — FundingRadar full backend smoke test
#  Run from the repo root: bash scripts/test_backend.sh
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

BASE="http://localhost:8000"
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
PASS=0
FAIL=0
WARN=0

pass() { ((PASS++)); echo -e "  ${GREEN}✅ PASS${NC} — $1"; }
fail() { ((FAIL++)); echo -e "  ${RED}❌ FAIL${NC} — $1"; }
warn() { ((WARN++)); echo -e "  ${YELLOW}⚠️  WARN${NC} — $1"; }
info() { echo -e "\n${CYAN}═══ $1 ═══${NC}"; }

# Helper: HTTP GET, check for 200
check_get() {
    local url="$1"
    local desc="$2"
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    if [ "$status" = "200" ]; then
        pass "$desc (HTTP $status)"
    else
        fail "$desc (HTTP $status)"
    fi
}

# Helper: HTTP GET, return body
get_json() {
    curl -s "$1" 2>/dev/null
}

# Helper: HTTP POST JSON, check for 200 or 201
check_post() {
    local url="$1"
    local data="$2"
    local desc="$3"
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$url" -H "Content-Type: application/json" -d "$data" 2>/dev/null || echo "000")
    if [ "$status" = "200" ] || [ "$status" = "201" ]; then
        pass "$desc (HTTP $status)"
    else
        fail "$desc (HTTP $status)"
    fi
}

# ═══════════════════════════════════════════════════════════════════
info "FASE 1: HEALTH CHECKS"
# ═══════════════════════════════════════════════════════════════════

check_get "$BASE/health" "GET /health"
check_get "$BASE/ready" "GET /ready (DB + Redis)"
check_get "$BASE/docs" "GET /docs (OpenAPI)"

# ═══════════════════════════════════════════════════════════════════
info "FASE 2: MONITORING ENDPOINTS"
# ═══════════════════════════════════════════════════════════════════

check_get "$BASE/collectors/status" "GET /collectors/status"

# Check that both collectors are running
COLL_STATUS=$(get_json "$BASE/collectors/status")
if echo "$COLL_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('hyperliquid',{}).get('running')==True" 2>/dev/null; then
    pass "Hyperliquid collector running"
else
    fail "Hyperliquid collector NOT running"
fi
if echo "$COLL_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('aster',{}).get('running')==True" 2>/dev/null; then
    pass "Aster collector running"
else
    fail "Aster collector NOT running"
fi

check_get "$BASE/service/status" "GET /service/status"

# Check that updates are coming in
SVC_STATUS=$(get_json "$BASE/service/status")
UPDATE_COUNT=$(echo "$SVC_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('update_count',0))" 2>/dev/null || echo "0")
if [ "$UPDATE_COUNT" -gt 0 ] 2>/dev/null; then
    pass "FundingService receiving updates (count=$UPDATE_COUNT)"
else
    warn "FundingService update_count=0 — collectors may still be warming up"
fi

check_get "$BASE/ws/status" "GET /ws/status"

# ═══════════════════════════════════════════════════════════════════
info "FASE 3: REDIS DATA CHECK"
# ═══════════════════════════════════════════════════════════════════

# Check Redis keys exist
REDIS_PASS="${REDIS_PASSWORD:-redisdev123}"
RANKED=$(docker compose exec -T redis redis-cli -a "$REDIS_PASS" GET "funding:ranked" 2>/dev/null | head -c 50)
if [ -n "$RANKED" ] && [ "$RANKED" != "(nil)" ]; then
    pass "Redis key 'funding:ranked' exists"
else
    warn "Redis key 'funding:ranked' empty — data may still be loading"
fi

ARB=$(docker compose exec -T redis redis-cli -a "$REDIS_PASS" GET "arbitrage:current" 2>/dev/null | head -c 50)
if [ -n "$ARB" ] && [ "$ARB" != "(nil)" ]; then
    pass "Redis key 'arbitrage:current' exists"
else
    warn "Redis key 'arbitrage:current' empty — may need 2+ exchanges with overlapping tokens"
fi

KEY_COUNT=$(docker compose exec -T redis redis-cli -a "$REDIS_PASS" KEYS "funding:latest:*" 2>/dev/null | wc -l | tr -d ' ')
if [ "$KEY_COUNT" -gt 0 ] 2>/dev/null; then
    pass "Redis has $KEY_COUNT funding:latest:* snapshot keys"
else
    warn "No funding:latest:* keys in Redis yet"
fi

# ═══════════════════════════════════════════════════════════════════
info "FASE 4: FUNDING RATE API"
# ═══════════════════════════════════════════════════════════════════

check_get "$BASE/api/v1/funding/rates" "GET /funding/rates (live)"

# Check response has data
RATES_RESP=$(get_json "$BASE/api/v1/funding/rates")
RATES_TOTAL=$(echo "$RATES_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null || echo "0")
if [ "$RATES_TOTAL" -gt 0 ] 2>/dev/null; then
    pass "Funding rates: $RATES_TOTAL tokens with live data"
else
    warn "Funding rates: 0 tokens (collectors still warming up?)"
fi

check_get "$BASE/api/v1/funding/rates?token=BTC" "GET /funding/rates?token=BTC"
check_get "$BASE/api/v1/funding/rates?exchanges=hyperliquid" "GET /funding/rates?exchanges=hyperliquid"
check_get "$BASE/api/v1/funding/rates?exchanges=aster" "GET /funding/rates?exchanges=aster"

# Token detail
TOKEN_RESP=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/funding/token/BTC" 2>/dev/null || echo "000")
if [ "$TOKEN_RESP" = "200" ]; then
    pass "GET /funding/token/BTC"
elif [ "$TOKEN_RESP" = "404" ]; then
    warn "GET /funding/token/BTC — 404 (BTC data may not exist yet)"
else
    fail "GET /funding/token/BTC (HTTP $TOKEN_RESP)"
fi

# ═══════════════════════════════════════════════════════════════════
info "FASE 5: ARBITRAGE API"
# ═══════════════════════════════════════════════════════════════════

check_get "$BASE/api/v1/arbitrage/opportunities" "GET /arbitrage/opportunities"

ARB_RESP=$(get_json "$BASE/api/v1/arbitrage/opportunities")
ARB_TOTAL=$(echo "$ARB_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null || echo "0")
if [ "$ARB_TOTAL" -gt 0 ] 2>/dev/null; then
    pass "Arbitrage: $ARB_TOTAL opportunities found"
    # Show top 3
    echo -e "  ${CYAN}Top 3 opportunities:${NC}"
    echo "$ARB_RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin).get('data', [])[:3]
for d in data:
    token = d.get('token','?')
    apr = d.get('net_apr_taker',0)
    le = d.get('long_leg',{}).get('exchange','?')
    se = d.get('short_leg',{}).get('exchange','?')
    print(f'    {token}: APR {apr:.2f}% — long {le} / short {se}')
" 2>/dev/null || true
else
    warn "Arbitrage: 0 opportunities (need 2+ exchanges with overlapping tokens)"
fi

# ═══════════════════════════════════════════════════════════════════
info "FASE 6: EXCHANGES API"
# ═══════════════════════════════════════════════════════════════════

check_get "$BASE/api/v1/exchanges" "GET /exchanges"

# ═══════════════════════════════════════════════════════════════════
info "FASE 7: SIMULATOR API"
# ═══════════════════════════════════════════════════════════════════

SIM_DATA='{
    "token": "BTC",
    "long_exchange": "hyperliquid",
    "short_exchange": "aster",
    "capital_usd": 10000,
    "days": 30,
    "fee_type": "taker",
    "slippage_pct": 0.05
}'

SIM_STATUS=$(curl -s -o /tmp/sim_resp.json -w "%{http_code}" -X POST "$BASE/api/v1/simulator/calculate" \
    -H "Content-Type: application/json" -d "$SIM_DATA" 2>/dev/null || echo "000")

if [ "$SIM_STATUS" = "200" ]; then
    pass "POST /simulator/calculate (HTTP $SIM_STATUS)"
    NET_PNL=$(python3 -c "import json; d=json.load(open('/tmp/sim_resp.json')); print(f'Net PnL: \${d[\"net_pnl_usd\"]:.2f}, APR: {d[\"net_apr\"]:.2f}%')" 2>/dev/null || echo "parse error")
    echo -e "  ${CYAN}  → $NET_PNL${NC}"
elif [ "$SIM_STATUS" = "404" ]; then
    warn "POST /simulator/calculate — 404 (BTC data on both exchanges not available yet)"
else
    fail "POST /simulator/calculate (HTTP $SIM_STATUS)"
fi

# ═══════════════════════════════════════════════════════════════════
info "FASE 8: AUTH API"
# ═══════════════════════════════════════════════════════════════════

# Register
REG_STATUS=$(curl -s -o /tmp/reg_resp.json -w "%{http_code}" -X POST "$BASE/api/v1/auth/register" \
    -H "Content-Type: application/json" \
    -d '{"email":"smoketest@test.com","password":"Test123456!"}' 2>/dev/null || echo "000")

if [ "$REG_STATUS" = "200" ] || [ "$REG_STATUS" = "201" ]; then
    pass "POST /auth/register"
elif [ "$REG_STATUS" = "409" ] || [ "$REG_STATUS" = "422" ]; then
    warn "POST /auth/register — user may already exist ($REG_STATUS)"
else
    fail "POST /auth/register (HTTP $REG_STATUS)"
fi

# Login
LOGIN_STATUS=$(curl -s -o /tmp/login_resp.json -w "%{http_code}" -X POST "$BASE/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email":"smoketest@test.com","password":"Test123456!"}' 2>/dev/null || echo "000")

if [ "$LOGIN_STATUS" = "200" ]; then
    pass "POST /auth/login"
    JWT_TOKEN=$(python3 -c "import json; print(json.load(open('/tmp/login_resp.json')).get('access_token',''))" 2>/dev/null || echo "")
    if [ -n "$JWT_TOKEN" ] && [ "$JWT_TOKEN" != "" ]; then
        pass "JWT token received"
        
        # Test /auth/me
        ME_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/auth/me" \
            -H "Authorization: Bearer $JWT_TOKEN" 2>/dev/null || echo "000")
        if [ "$ME_STATUS" = "200" ]; then
            pass "GET /auth/me with JWT"
        else
            fail "GET /auth/me with JWT (HTTP $ME_STATUS)"
        fi
        
        # Generate API key
        APIKEY_STATUS=$(curl -s -o /tmp/apikey_resp.json -w "%{http_code}" -X POST "$BASE/api/v1/auth/api-key" \
            -H "Authorization: Bearer $JWT_TOKEN" 2>/dev/null || echo "000")
        if [ "$APIKEY_STATUS" = "200" ] || [ "$APIKEY_STATUS" = "201" ]; then
            pass "POST /auth/api-key"
            API_KEY=$(python3 -c "import json; print(json.load(open('/tmp/apikey_resp.json')).get('api_key',''))" 2>/dev/null || echo "")
            if [ -n "$API_KEY" ]; then
                # Test with API key
                APIKEY_TEST=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/funding/rates" \
                    -H "X-API-Key: $API_KEY" 2>/dev/null || echo "000")
                if [ "$APIKEY_TEST" = "200" ]; then
                    pass "GET /funding/rates with X-API-Key"
                else
                    fail "GET /funding/rates with X-API-Key (HTTP $APIKEY_TEST)"
                fi
            fi
        else
            fail "POST /auth/api-key (HTTP $APIKEY_STATUS)"
        fi
    else
        fail "No JWT token in login response"
    fi
else
    fail "POST /auth/login (HTTP $LOGIN_STATUS)"
fi

# ═══════════════════════════════════════════════════════════════════
info "FASE 9: WEBSOCKET TEST (quick 10s)"
# ═══════════════════════════════════════════════════════════════════

# Quick Python WS test
python3 -c "
import asyncio, json, sys

async def test():
    try:
        import websockets
    except ImportError:
        print('  ⚠️  websockets not installed locally — skipping WS test')
        return False
    
    try:
        uri = 'ws://localhost:8000/ws/funding'
        async with websockets.connect(uri, open_timeout=5) as ws:
            # Should get connected message
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get('type') == 'connected':
                print(f'  ✅ PASS — WS connected (tier={msg.get(\"tier\")})')
            
            # Subscribe
            await ws.send(json.dumps({'action':'subscribe','channels':['funding','arbitrage']}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get('type') == 'subscribed':
                print(f'  ✅ PASS — WS subscribed to {msg.get(\"channels\")}')
            
            # Wait for one data message (up to 15s)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                msg = json.loads(raw)
                mtype = msg.get('type','?')
                print(f'  ✅ PASS — WS received message type={mtype}')
            except asyncio.TimeoutError:
                print('  ⚠️  WARN — No data message in 15s (may need more time)')
            
            return True
    except Exception as e:
        print(f'  ❌ FAIL — WS error: {e}')
        return False

asyncio.run(test())
" 2>/dev/null || warn "WebSocket test skipped (install websockets: pip install websockets)"

# ═══════════════════════════════════════════════════════════════════
info "FASE 10: DATABASE PERSISTENCE CHECK"
# ═══════════════════════════════════════════════════════════════════

ROW_COUNT=$(docker compose exec -T postgres psql -U funding_user -d funding_radar -t -c "SELECT COUNT(*) FROM funding_rates;" 2>/dev/null | tr -d ' \n' || echo "0")
if [ "$ROW_COUNT" -gt 0 ] 2>/dev/null; then
    pass "TimescaleDB has $ROW_COUNT funding_rate rows"
else
    warn "TimescaleDB has 0 rows (persist job runs every 30s — wait and retry)"
fi

EXCHANGE_COUNT=$(docker compose exec -T postgres psql -U funding_user -d funding_radar -t -c "SELECT COUNT(*) FROM exchanges;" 2>/dev/null | tr -d ' \n' || echo "0")
if [ "$EXCHANGE_COUNT" -ge 2 ] 2>/dev/null; then
    pass "DB has $EXCHANGE_COUNT exchanges seeded"
else
    fail "DB has $EXCHANGE_COUNT exchanges (expected ≥2 — run seed_exchanges.py)"
fi

TOKEN_COUNT=$(docker compose exec -T postgres psql -U funding_user -d funding_radar -t -c "SELECT COUNT(*) FROM tokens;" 2>/dev/null | tr -d ' \n' || echo "0")
if [ "$TOKEN_COUNT" -ge 10 ] 2>/dev/null; then
    pass "DB has $TOKEN_COUNT tokens seeded"
else
    fail "DB has $TOKEN_COUNT tokens (expected ≥10 — run seed_exchanges.py)"
fi

# Check hypertable
HT_CHECK=$(docker compose exec -T postgres psql -U funding_user -d funding_radar -t -c "SELECT hypertable_name FROM timescaledb_information.hypertables WHERE hypertable_name = 'funding_rates';" 2>/dev/null | tr -d ' \n' || echo "")
if [ "$HT_CHECK" = "funding_rates" ]; then
    pass "TimescaleDB hypertable 'funding_rates' active"
else
    warn "TimescaleDB hypertable not detected (extension may not be loaded)"
fi

# ═══════════════════════════════════════════════════════════════════
info "FASE 11: HISTORICAL DATA (needs data accumulation)"
# ═══════════════════════════════════════════════════════════════════

HIST_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/funding/history/BTC?exchange=hyperliquid&timeframe=24h&interval=1h" 2>/dev/null || echo "000")
if [ "$HIST_STATUS" = "200" ]; then
    HIST_COUNT=$(get_json "$BASE/api/v1/funding/history/BTC?exchange=hyperliquid&timeframe=24h&interval=1h" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "0")
    if [ "$HIST_COUNT" -gt 0 ] 2>/dev/null; then
        pass "Historical BTC data: $HIST_COUNT time buckets"
    else
        warn "Historical endpoint works but 0 buckets (needs more accumulated data)"
    fi
else
    warn "GET /funding/history/BTC (HTTP $HIST_STATUS — may need seeded token/exchange)"
fi

# ═══════════════════════════════════════════════════════════════════
info "RESULTADOS"
# ═══════════════════════════════════════════════════════════════════

echo ""
echo -e "  ${GREEN}Passed:  $PASS${NC}"
echo -e "  ${YELLOW}Warnings: $WARN${NC}"
echo -e "  ${RED}Failed:  $FAIL${NC}"
echo ""

if [ "$FAIL" -eq 0 ]; then
    echo -e "${GREEN}🎉 All critical tests passed!${NC}"
    if [ "$WARN" -gt 0 ]; then
        echo -e "${YELLOW}   Some warnings — likely just warm-up time needed.${NC}"
        echo -e "${YELLOW}   Wait 2-3 minutes and run again to clear warnings.${NC}"
    fi
    exit 0
else
    echo -e "${RED}⚠️  $FAIL test(s) failed — check the output above.${NC}"
    exit 1
fi
