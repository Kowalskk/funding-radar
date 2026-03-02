# test_backend.ps1 - FundingRadar full backend smoke test (PowerShell)
# Run from the repo root: .\scripts\test_backend.ps1

$ErrorActionPreference = "Continue"

$BASE = "http://localhost:8000"
$PASS = 0
$FAIL = 0
$WARN = 0

function Pass($msg) { $script:PASS++; Write-Host "  PASS - $msg" -ForegroundColor Green }
function Fail($msg) { $script:FAIL++; Write-Host "  FAIL - $msg" -ForegroundColor Red }
function Warn($msg) { $script:WARN++; Write-Host "  WARN - $msg" -ForegroundColor Yellow }
function Info($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

function Check-Get($url, $desc) {
    try {
        $resp = Invoke-WebRequest -Uri $url -Method GET -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { Pass "$desc (HTTP $($resp.StatusCode))" }
        else { Fail "$desc (HTTP $($resp.StatusCode))" }
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code) { Fail "$desc (HTTP $code)" }
        else { Fail "$desc (connection error)" }
    }
}

function Get-Json($url) {
    try {
        $resp = Invoke-WebRequest -Uri $url -Method GET -UseBasicParsing -ErrorAction Stop
        return $resp.Content | ConvertFrom-Json
    } catch { return $null }
}

# === FASE 1: HEALTH CHECKS ===
Info "FASE 1: HEALTH CHECKS"
Check-Get "$BASE/health" "GET /health"
Check-Get "$BASE/ready" "GET /ready (DB + Redis)"
Check-Get "$BASE/docs" "GET /docs (OpenAPI)"

# === FASE 2: MONITORING ===
Info "FASE 2: MONITORING ENDPOINTS"
Check-Get "$BASE/collectors/status" "GET /collectors/status"

$collStatus = Get-Json "$BASE/collectors/status"
if ($collStatus) {
    if ($collStatus.hyperliquid.running -eq $true) { Pass "Hyperliquid collector running" }
    else { Fail "Hyperliquid collector NOT running" }

    if ($collStatus.aster.running -eq $true) { Pass "Aster collector running" }
    else { Fail "Aster collector NOT running" }
}

Check-Get "$BASE/service/status" "GET /service/status"

$svcStatus = Get-Json "$BASE/service/status"
if ($svcStatus -and $svcStatus.update_count -gt 0) {
    Pass "FundingService receiving updates (count=$($svcStatus.update_count))"
} else {
    Warn "FundingService update_count=0 - collectors may still be warming up"
}

Check-Get "$BASE/ws/status" "GET /ws/status"

# === FASE 3: REDIS DATA ===
Info "FASE 3: REDIS DATA CHECK"
$redisPass = "redisdev123"
$ranked = docker compose exec -T redis redis-cli -a $redisPass GET "funding:ranked" 2>$null
if ($ranked -and $ranked -ne "(nil)") {
    Pass "Redis key funding:ranked exists"
} else {
    Warn "Redis key funding:ranked empty - data may still be loading"
}

$arb = docker compose exec -T redis redis-cli -a $redisPass GET "arbitrage:current" 2>$null
if ($arb -and $arb -ne "(nil)") {
    Pass "Redis key arbitrage:current exists"
} else {
    Warn "Redis key arbitrage:current empty - may need 2+ exchanges"
}

# === FASE 4: FUNDING RATE API ===
Info "FASE 4: FUNDING RATE API"
Check-Get "$BASE/api/v1/funding/rates" "GET /funding/rates (live)"

$ratesResp = Get-Json "$BASE/api/v1/funding/rates"
if ($ratesResp -and $ratesResp.total -gt 0) {
    Pass "Funding rates: $($ratesResp.total) tokens with live data"
} else {
    Warn "Funding rates: 0 tokens (collectors still warming up?)"
}

Check-Get "$BASE/api/v1/funding/rates?token=BTC" "GET /funding/rates?token=BTC"
Check-Get "$BASE/api/v1/funding/rates?exchanges=hyperliquid" "GET /funding/rates?exchanges=hyperliquid"
Check-Get "$BASE/api/v1/funding/rates?exchanges=aster" "GET /funding/rates?exchanges=aster"

try {
    $tokenResp = Invoke-WebRequest -Uri "$BASE/api/v1/funding/token/BTC" -UseBasicParsing -ErrorAction Stop
    Pass "GET /funding/token/BTC"
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 404) { Warn "GET /funding/token/BTC - 404 (data may not exist yet)" }
    else { Fail "GET /funding/token/BTC (HTTP $code)" }
}

# === FASE 5: ARBITRAGE API ===
Info "FASE 5: ARBITRAGE API"
Check-Get "$BASE/api/v1/arbitrage/opportunities" "GET /arbitrage/opportunities"

$arbResp = Get-Json "$BASE/api/v1/arbitrage/opportunities"
if ($arbResp -and $arbResp.total -gt 0) {
    Pass "Arbitrage: $($arbResp.total) opportunities found"
    Write-Host "  Top 3 opportunities:" -ForegroundColor Cyan
    $top = if ($arbResp.data) { $arbResp.data | Select-Object -First 3 } else { @() }
    foreach ($d in $top) {
        $token = $d.token; $apr = $d.net_apr_taker
        $le = $d.long_leg.exchange; $se = $d.short_leg.exchange
        Write-Host "    ${token}: APR ${apr}% - long $le / short $se" -ForegroundColor Cyan
    }
} else {
    Warn "Arbitrage: 0 opportunities"
}

Start-Sleep -Milliseconds 2000
# === FASE 6: EXCHANGES API ===
Info "FASE 6: EXCHANGES API"
Check-Get "$BASE/api/v1/exchanges" "GET /exchanges"

Start-Sleep -Milliseconds 2000
# === FASE 7: SIMULATOR API ===
Info "FASE 7: SIMULATOR API"

$simData = @{
    token = "BTC"
    long_exchange = "hyperliquid"
    short_exchange = "aster"
    capital_usd = 10000
    days = 30
    fee_type = "taker"
    slippage_pct = 0.05
} | ConvertTo-Json

try {
    $simResp = Invoke-WebRequest -Uri "$BASE/api/v1/simulator/calculate" -Method POST `
        -Body $simData -ContentType "application/json" -UseBasicParsing -ErrorAction Stop
    Pass "POST /simulator/calculate (HTTP $($simResp.StatusCode))"
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 404) { Warn "POST /simulator/calculate - 404 (data not available yet)" }
    elseif ($code -eq 422) { Warn "POST /simulator/calculate - 422 (validation)" }
    else { Fail "POST /simulator/calculate (HTTP $code)" }
}

# === FASE 8: AUTH API ===
Info "FASE 8: AUTH API"

$regData = '{"email":"smoketest@test.com","password":"Test123456!"}'
try {
    $regResp = Invoke-WebRequest -Uri "$BASE/api/v1/auth/register" -Method POST `
        -Body $regData -ContentType "application/json" -UseBasicParsing -ErrorAction Stop
    Pass "POST /auth/register"
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 409 -or $code -eq 422) { Warn "POST /auth/register - user may already exist ($code)" }
    else { Fail "POST /auth/register (HTTP $code)" }
}

$loginData = '{"email":"smoketest@test.com","password":"Test123456!"}'
$jwtToken = $null
try {
    $loginResp = Invoke-WebRequest -Uri "$BASE/api/v1/auth/login" -Method POST `
        -Body $loginData -ContentType "application/json" -UseBasicParsing -ErrorAction Stop
    Pass "POST /auth/login"
    $loginJson = $loginResp.Content | ConvertFrom-Json
    $jwtToken = $loginJson.access_token
    if ($jwtToken) { Pass "JWT token received" }
    else { Fail "No JWT token in login response" }
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    Fail "POST /auth/login (HTTP $code)"
}

if ($jwtToken) {
    try {
        $meResp = Invoke-WebRequest -Uri "$BASE/api/v1/auth/me" -UseBasicParsing `
            -Headers @{ Authorization = "Bearer $jwtToken" } -ErrorAction Stop
        Pass "GET /auth/me with JWT"
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        Fail "GET /auth/me with JWT (HTTP $code)"
    }

    try {
        $akResp = Invoke-WebRequest -Uri "$BASE/api/v1/auth/api-key" -Method POST `
            -Headers @{ Authorization = "Bearer $jwtToken" } -UseBasicParsing -ErrorAction Stop
        Pass "POST /auth/api-key"
        $akJson = $akResp.Content | ConvertFrom-Json
        $apiKey = $akJson.api_key
        if ($apiKey) {
            try {
                $akTest = Invoke-WebRequest -Uri "$BASE/api/v1/funding/rates" `
                    -Headers @{ "X-API-Key" = $apiKey } -UseBasicParsing -ErrorAction Stop
                Pass "GET /funding/rates with X-API-Key"
            } catch {
                $code = $_.Exception.Response.StatusCode.value__
                Fail "GET /funding/rates with X-API-Key (HTTP $code)"
            }
        }
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        Fail "POST /auth/api-key (HTTP $code)"
    }
}
Start-Sleep -Milliseconds 2000

# === FASE 9: DATABASE PERSISTENCE CHECK ===
Info "FASE 9: DATABASE PERSISTENCE CHECK"

$exRaw = docker compose exec -T postgres psql -U funding_user -d funding_radar -t -c "SELECT COUNT(*) FROM exchanges;" 2>$null
$exCount = if ($exRaw) { ($exRaw | Out-String) -replace '[^0-9]', '' } else { "" }
$exCount = if ($exCount -match '^\d+$') { [int]$exCount } else { -1 }
try {
    if ($exCount -ge 2) {
        Pass "DB has $exCount exchanges seeded"
    } else {
        Fail "DB has $exCount exchanges (expected >= 2)"
    }
} catch { Fail "Could not query exchanges count" }

$tokRaw = docker compose exec -T postgres psql -U funding_user -d funding_radar -t -c "SELECT COUNT(*) FROM tokens;" 2>$null
$tokCount = if ($tokRaw) { ($tokRaw | Out-String) -replace '[^0-9]', '' } else { "" }
$tokCount = if ($tokCount -match '^\d+$') { [int]$tokCount } else { -1 }
try {
    if ($tokCount -ge 10) {
        Pass "DB has $tokCount tokens seeded"
    } else {
        Fail "DB has $tokCount tokens (expected >= 10)"
    }
} catch { Fail "Could not query tokens count" }

# === RESULTS ===
Info "RESULTADOS"

Write-Host ""
Write-Host "  Passed:   $PASS" -ForegroundColor Green
Write-Host "  Warnings: $WARN" -ForegroundColor Yellow
Write-Host "  Failed:   $FAIL" -ForegroundColor Red
Write-Host ""

if ($FAIL -eq 0) {
    Write-Host "All critical tests passed!" -ForegroundColor Green
    if ($WARN -gt 0) {
        Write-Host "   Some warnings - likely just warm-up time needed." -ForegroundColor Yellow
        Write-Host "   Wait 2-3 minutes and run again to clear warnings." -ForegroundColor Yellow
    }
    exit 0
} else {
    Write-Host "$FAIL test(s) failed - check the output above." -ForegroundColor Red
    exit 1
}
