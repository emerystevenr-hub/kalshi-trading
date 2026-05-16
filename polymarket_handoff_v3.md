# POLYMARKET PROJECT — HANDOFF V3
_Written 2026-04-16. Paper MM killed. Going live with real $1 orders on Dublin VPS._

## BOTTOM LINE UP FRONT

Arb is dead. Paper trading is also dead (can't simulate maker queue fills from book snapshots alone). We are going directly to live trading with $1 orders on a Dublin VPS to get real fill data. If fills are positive after 48 hours, we ramp to $50k.

**Capital staged:** $200 USDC on Polygon for $1 test orders. Scale to $50k on proof.
**Deployment target:** Dublin VPS (AWS eu-west-1 or equivalent), sub-2ms to London matching engine.
**Status:** `live_mm.py` built. Waiting on Steve to provision VPS + wallet + API creds.

---

## WHY NOT PAPER TRADE

Paper trading a maker strategy on a CLOB is fundamentally broken:

1. **Can't simulate queue position.** Polymarket's CLOB is FIFO. Paper MM assumes front-of-queue, reality is back-of-queue. Every paper fill is a lie.
2. **WS only delivers top-of-book price changes.** If a taker sells 500 shares into a 10,000-share bid level, the bid price doesn't change — just the size. Our paper MM never sees it. Zero fills in 14+ hours on markets doing $44k/day volume.
3. **Can't distinguish maker cancels from taker fills.** Both show up as book level changes. No way to measure adverse selection from book data alone.

Real $1 orders in the real queue = real fill data in hours, not weeks of debugging fake fills.

---

## WHAT WORKS / WHAT DOESN'T

### DEAD (don't revisit)
- N-way Dutching (ask-side + bid-side) — symmetric MM vig
- Rule 4 ladder monotonicity — 1 violation, $15, 1.1%
- Rule 2 correlated outcomes — dead after taker fees
- MLB moneyline/futures — model too weak, variance dominates
- Paper MM on dead political markets — 0 fills in 14 hours
- Paper MM on active markets — broken fill detection, 0 fills in 19 min

### LIVE
- **Live market making with real limit orders, deployed to Dublin VPS**

---

## INFRASTRUCTURE SPEC

### Why Dublin
Polymarket's CLOB matching engine is AWS London (eu-west-2). London IPs are geo-blocked from trading. Dublin (eu-west-1) is on the same regional fiber backbone — sub-2ms ping to the matching engine, unrestricted EU jurisdiction.

### VPS Requirements
- **Provider:** AWS (eu-west-1), DigitalOcean (LON1/DUB), Vultr, or equivalent
- **Region:** Dublin, Ireland — NON-NEGOTIABLE
- **Spec:** Minimal — 1 vCPU, 1GB RAM, 20GB SSD. This is I/O bound not compute bound.
- **OS:** Ubuntu 24.04 LTS
- **Cost:** ~$5-12/month
- **Networking:** Outbound HTTPS to clob.polymarket.com + WSS to ws-subscriptions-clob.polymarket.com

### Wallet / API Setup (Steve's side)
1. MetaMask or similar wallet
2. Bridge $200 USDC to Polygon network (chain ID 137)
3. Connect wallet to polymarket.com
4. Generate CLOB API credentials (Settings → API)
5. Export private key from MetaMask (needed for order signing)

### Server Setup (after VPS is provisioned)
```bash
# SSH in
ssh root@<dublin-ip>

# System
apt update && apt install -y python3 python3-pip

# Code
mkdir -p /opt/mm && cd /opt/mm
# scp or git clone the files from ~/Documents/

# Dependencies
pip3 install py-clob-client websocket-client --break-system-packages

# Secrets (create this file, chmod 400)
cat > /etc/mm/secrets << 'EOF'
export POLY_API_KEY='your-api-key'
export POLY_API_SECRET='your-api-secret'
export POLY_API_PASSPHRASE='your-api-passphrase'
export POLY_PRIVATE_KEY='0xyour-private-key'
export POLY_FUNDER='0xyour-wallet-address'
export CLIP_SIZE_USD='1.0'
export MAX_INVENTORY_USD='50.0'
export DAILY_LOSS_LIMIT_USD='25.0'
EOF
chmod 400 /etc/mm/secrets

# Run manually first
source /etc/mm/secrets && cd /opt/mm && python3 live_mm.py

# Once confirmed working, set up systemd:
cat > /etc/systemd/system/mm.service << 'EOF'
[Unit]
Description=Polymarket Market Maker
After=network.target

[Service]
Type=simple
EnvironmentFile=/etc/mm/secrets
WorkingDirectory=/opt/mm
ExecStart=/usr/bin/python3 /opt/mm/live_mm.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl enable mm && systemctl start mm
journalctl -u mm -f   # tail logs
```

---

## CURRENT FILES IN `~/Documents/`

### Active (deploy these to VPS)
- `live_mm.py` — **LIVE market maker. Real orders. Real money.** Reads universe, posts maker limit orders, polls for fills, manages inventory + risk.
- `mm_event_curator.py` — v3 volume-first curator. Run weekly to refresh universe.
- `mm_universe.json` — curated event list. Copy to VPS.

### Reference (keep locally, don't deploy)
- `paper_mm.py` — paper MM. Dead. Kept for code reference only.
- `inequality_scanner.py` — Rule 4 scanner. Dead strategy.
- `correlated_scanner.py` — Rule 2 scanner. Dead strategy.
- All MLB files, sniper.py, execution.py, config.py — deprecated.

---

## LIVE DEPLOYMENT SEQUENCE

1. **Steve provisions Dublin VPS** + wallet + API creds
2. **SSH into VPS**, install deps, copy code + universe
3. **Source secrets**, run `live_mm.py` manually in tmux/screen
4. **Watch first 30 minutes** — confirm orders posting, WS connected, fills detected
5. **Let it run 48 hours** — measure fills/hr, realized P&L, adverse selection
6. **Go/no-go on ramp:**
   - Fills > 3/hr + realized > $0 → ramp CLIP_SIZE_USD to $10, then $100
   - Fills > 0 but realized < $0 → tune spread/quote strategy
   - Zero fills → debug order placement / check queue depth

---

## RISK CONTROLS (hardcoded in live_mm.py)

- `CLIP_SIZE_USD = 1.0` — $1 per order. Max loss per fill = $1.
- `MAX_INVENTORY_USD = 50.0` — stops buying if one-sided inventory exceeds $50
- `DAILY_LOSS_LIMIT_USD = 25.0` — auto-cancels all orders, 1-hour cooldown
- Graceful shutdown on SIGINT/SIGTERM — cancels all open orders before exit
- All secrets via env vars, never hardcoded

---

## RAMP PLAN (after first 48hr proves fills work)

| Phase | Clip Size | Max Inventory | Daily Loss Limit | Duration |
|---|---|---|---|---|
| Test | $1 | $50 | $25 | 48 hours |
| Prove | $10 | $500 | $100 | 1 week |
| Scale | $100 | $5,000 | $500 | 2 weeks |
| Full | $500 | $10,000 | $1,500 | Ongoing |

Full deployment at $50k capital: $500 clips, $10k max per market, $1.5k daily stop.

---

## OPEN QUESTIONS

1. ~~Polymarket account status~~ → Steve setting up wallet + USDC bridge
2. ~~VPS location~~ → Dublin confirmed (eu-west-1)
3. Telegram/Pushover alerting — build after first successful fills
4. Fair-value anchor (Kalshi/Pinnacle) — build after ramp to $100 clips
5. News-driven auto-kill — build after fair-value anchor

---

## HOW TO RESUME IN NEW THREAD

1. Read this doc (`polymarket_handoff_v3.md`)
2. Ask Steve: "Is the Dublin VPS up? Do you have API creds?"
3. If yes: walk through deployment sequence above
4. If no: help him provision VPS + wallet setup
5. First milestone: see `🟢 FILL BUY` in the live_mm.py logs

Do not re-test paper trading. Do not re-scan for arbitrage. Go live.
