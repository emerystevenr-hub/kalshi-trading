"""
LIVE MARKET MAKER — Polymarket CLOB

Real limit orders on the Polymarket CLOB via py-clob-client.
Designed for deployment on a Dublin (eu-west-1) VPS for sub-2ms latency
to the London matching engine.

Architecture:
  - WS thread:      real-time book updates, triggers reprice/cancel decisions
  - Order manager:   tracks open orders per token, cancels stale quotes,
                     submits new limit orders when spread is favorable
  - Risk manager:    enforces inventory caps, daily loss limits, auto-pause
  - Main thread:     periodic health checks, logging, console output

Security:
  - Private key loaded from env var POLY_PRIVATE_KEY (never hardcoded)
  - API key from env var POLY_API_KEY
  - API secret from env var POLY_API_SECRET
  - API passphrase from env var POLY_API_PASSPHRASE
  - All secrets should be in /etc/mm/secrets on the VPS, sourced by systemd

Order lifecycle:
  1. On WS book update: compute fair mid, determine quote prices
  2. If our open order is >1 tick from desired price: CANCEL then repost
  3. If no open order on a side: POST new limit order
  4. If spread collapses below MIN_SPREAD: cancel both sides, wait
  5. On fill notification (via user WS channel): update inventory, log, check risk

CRITICAL: This places REAL orders with REAL money. Start with CLIP_SIZE_USD=1.0
"""

import csv
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
except ImportError:
    print("Missing dependency. Run:")
    print("  pip install py-clob-client --break-system-packages")
    sys.exit(1)

try:
    import websocket
except ImportError:
    print("Missing dependency. Run:")
    print("  pip install websocket-client --break-system-packages")
    sys.exit(1)

# ── ENV CONFIG ────────────────────────────────────────────────────────
POLY_HOST = os.environ.get("POLY_HOST", "https://clob.polymarket.com")
POLY_API_KEY = os.environ.get("POLY_API_KEY", "")
POLY_API_SECRET = os.environ.get("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.environ.get("POLY_API_PASSPHRASE", "")
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "")
POLY_FUNDER = os.environ.get("POLY_FUNDER", "")  # your wallet address
CHAIN_ID = 137  # Polygon mainnet

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UNIVERSE_PATH = os.environ.get("MM_UNIVERSE", os.path.join(os.path.dirname(__file__), "mm_universe.json"))
TRADES_LOG = os.path.join(os.path.dirname(__file__), "live_mm_trades.csv")
POSITIONS_LOG = os.path.join(os.path.dirname(__file__), "live_mm_positions.csv")

# ── TRADING CONFIG ────────────────────────────────────────────────────
CLIP_SIZE_USD = float(os.environ.get("CLIP_SIZE_USD", "1.0"))  # START AT $1
TICK = 0.01
MIN_SPREAD_TO_QUOTE = 0.02
MAX_INVENTORY_USD = float(os.environ.get("MAX_INVENTORY_USD", "50.0"))
DAILY_LOSS_LIMIT_USD = float(os.environ.get("DAILY_LOSS_LIMIT_USD", "25.0"))
REPRICE_THRESHOLD_TICKS = 1    # cancel + repost if book moves >=N ticks from our order
MID_RANGE = (0.05, 0.95)

# ── LOGGING ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live_mm")


# ──────────────────────────────────────────────────────────────────────

@dataclass
class OpenOrder:
    order_id: str
    side: str            # "BUY" or "SELL"
    price: float
    size: float          # in shares
    token_id: str
    posted_at: float     # time.time()


@dataclass
class MarketState:
    event_title: str
    label: str
    yes_token: str
    no_token: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    our_buy_order: Optional[OpenOrder] = None
    our_sell_order: Optional[OpenOrder] = None
    inventory: float = 0.0          # shares of YES token held
    cost_basis: float = 0.0
    realized_pnl: float = 0.0
    fills_buy: int = 0
    fills_sell: int = 0
    updates: int = 0


STATES: Dict[str, MarketState] = {}  # keyed by yes_token
_LOCK = threading.Lock()
_STOP = False
_START_TIME = time.time()
_CLIENT: Optional[ClobClient] = None


def handle_stop(signum, frame):
    global _STOP
    _STOP = True
    log.info("shutdown signal received — cancelling all orders...")


signal.signal(signal.SIGINT, handle_stop)
signal.signal(signal.SIGTERM, handle_stop)


# ──────────────────────────────────────────────────────────────────────
# INIT
# ──────────────────────────────────────────────────────────────────────

def validate_env():
    missing = []
    for var in ["POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE",
                "POLY_PRIVATE_KEY", "POLY_FUNDER"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        log.error(f"Missing env vars: {', '.join(missing)}")
        log.error("Set these in /etc/mm/secrets or export them before running.")
        log.error("Example:")
        log.error("  export POLY_API_KEY='your-key'")
        log.error("  export POLY_API_SECRET='your-secret'")
        log.error("  export POLY_API_PASSPHRASE='your-passphrase'")
        log.error("  export POLY_PRIVATE_KEY='0xyour-private-key'")
        log.error("  export POLY_FUNDER='0xyour-wallet-address'")
        sys.exit(1)


def init_client() -> ClobClient:
    client = ClobClient(
        host=POLY_HOST,
        key=POLY_API_KEY,
        chain_id=CHAIN_ID,
        funder=POLY_FUNDER,
        signature_type=2,
        private_key=POLY_PRIVATE_KEY,
    )
    # Set API creds for authenticated endpoints
    client.set_api_creds(client.create_or_derive_api_creds())
    log.info(f"CLOB client initialized — host={POLY_HOST} funder={POLY_FUNDER[:10]}...")
    return client


def init_universe() -> List[str]:
    with open(UNIVERSE_PATH) as f:
        universe = json.load(f)
    tokens = []
    for ev in universe["events"]:
        for m in ev["markets"]:
            spread = m.get("spread", 0)
            if spread < MIN_SPREAD_TO_QUOTE:
                continue
            mid = m.get("mid", 0)
            if mid < MID_RANGE[0] or mid > MID_RANGE[1]:
                continue
            token = m["yes_token"]
            STATES[token] = MarketState(
                event_title=ev["event_title"],
                label=m.get("label") or m.get("question") or "",
                yes_token=m["yes_token"],
                no_token=m.get("no_token", ""),
                best_bid=m.get("best_bid", 0),
                best_ask=m.get("best_ask", 0),
            )
            tokens.append(token)
    log.info(f"universe loaded: {len(tokens)} tokens")
    return tokens


def ensure_logs():
    for path, header in [
        (TRADES_LOG, ["timestamp", "event", "label", "side", "price",
                      "size_shares", "size_usd", "order_id",
                      "inventory_after", "realized_pnl"]),
        (POSITIONS_LOG, ["timestamp", "event", "label", "inventory",
                         "cost_basis", "last_mid", "realized_pnl",
                         "unrealized_pnl", "fills_buy", "fills_sell"]),
    ]:
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(header)


# ──────────────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ──────────────────────────────────────────────────────────────────────

def place_order(token_id: str, side: str, price: float, size_usd: float) -> Optional[OpenOrder]:
    """Place a limit order. Returns OpenOrder on success, None on failure."""
    size_shares = size_usd / price
    try:
        order_args = OrderArgs(
            price=price,
            size=size_shares,
            side=side,
            token_id=token_id,
        )
        resp = _CLIENT.create_and_post_order(order_args)
        order_id = resp.get("orderID") or resp.get("id") or str(resp)
        log.info(f"ORDER PLACED  {side} {size_shares:.1f}sh @ {price:.3f}  "
                 f"token={token_id[:12]}...  id={order_id}")
        return OpenOrder(
            order_id=order_id, side=side, price=price,
            size=size_shares, token_id=token_id, posted_at=time.time(),
        )
    except Exception as e:
        log.warning(f"ORDER FAILED  {side} @ {price:.3f}: {e}")
        return None


def cancel_order(order: OpenOrder) -> bool:
    """Cancel an open order. Returns True on success."""
    try:
        _CLIENT.cancel(order.order_id)
        log.info(f"ORDER CANCELLED  {order.side} @ {order.price:.3f}  id={order.order_id}")
        return True
    except Exception as e:
        log.warning(f"CANCEL FAILED  id={order.order_id}: {e}")
        return False


def cancel_all_orders():
    """Emergency: cancel every open order across all markets."""
    log.warning("CANCELLING ALL OPEN ORDERS")
    with _LOCK:
        for s in STATES.values():
            if s.our_buy_order:
                cancel_order(s.our_buy_order)
                s.our_buy_order = None
            if s.our_sell_order:
                cancel_order(s.our_sell_order)
                s.our_sell_order = None


# ──────────────────────────────────────────────────────────────────────
# QUOTING LOGIC
# ──────────────────────────────────────────────────────────────────────

def desired_quotes(s: MarketState) -> Tuple[Optional[float], Optional[float]]:
    """Compute ideal buy and sell prices given current book."""
    bid, ask = s.best_bid, s.best_ask
    if bid <= 0 or ask <= 0:
        return (None, None)
    spread = ask - bid
    if spread < MIN_SPREAD_TO_QUOTE:
        return (None, None)
    mid = (bid + ask) / 2.0
    if mid < MID_RANGE[0] or mid > MID_RANGE[1]:
        return (None, None)

    # Spread-dependent quoting:
    if spread <= 0.02 + 1e-9:
        buy_px = round(bid, 2)          # join queue
        sell_px = round(ask, 2)
    else:
        buy_px = round(bid + TICK, 2)   # post inside
        sell_px = round(ask - TICK, 2)

    # Inventory guard: don't buy if overweight
    inv_usd = s.inventory * ask
    buy_px_out = buy_px if inv_usd < MAX_INVENTORY_USD else None
    sell_px_out = sell_px if s.inventory > 0 else None

    return (buy_px_out, sell_px_out)


def manage_quotes(token: str):
    """Check if our open orders need repricing, cancellation, or posting."""
    s = STATES.get(token)
    if not s:
        return

    want_buy, want_sell = desired_quotes(s)

    # ── BUY side ──────────────────────────────────────────────────
    if s.our_buy_order:
        if want_buy is None:
            # shouldn't be quoting — cancel
            cancel_order(s.our_buy_order)
            s.our_buy_order = None
        elif abs(s.our_buy_order.price - want_buy) >= TICK * REPRICE_THRESHOLD_TICKS:
            # stale — cancel and repost
            cancel_order(s.our_buy_order)
            s.our_buy_order = place_order(token, "BUY", want_buy, CLIP_SIZE_USD)
    else:
        if want_buy is not None:
            s.our_buy_order = place_order(token, "BUY", want_buy, CLIP_SIZE_USD)

    # ── SELL side ─────────────────────────────────────────────────
    if s.our_sell_order:
        if want_sell is None:
            cancel_order(s.our_sell_order)
            s.our_sell_order = None
        elif abs(s.our_sell_order.price - want_sell) >= TICK * REPRICE_THRESHOLD_TICKS:
            cancel_order(s.our_sell_order)
            s.our_sell_order = place_order(token, "SELL", want_sell, CLIP_SIZE_USD)
    else:
        if want_sell is not None:
            s.our_sell_order = place_order(token, "SELL", want_sell, CLIP_SIZE_USD)


# ──────────────────────────────────────────────────────────────────────
# FILL HANDLING
# ──────────────────────────────────────────────────────────────────────

def handle_fill(token: str, side: str, price: float, size_shares: float, order_id: str):
    """Process a confirmed fill from the exchange."""
    s = STATES.get(token)
    if not s:
        return

    with _LOCK:
        if side == "BUY":
            old_inv = s.inventory
            new_inv = old_inv + size_shares
            s.cost_basis = (s.cost_basis * old_inv + price * size_shares) / new_inv if new_inv > 0 else 0
            s.inventory = new_inv
            s.fills_buy += 1
            s.our_buy_order = None  # order consumed
            log.info(f"🟢 FILL BUY  {size_shares:.1f}sh @ {price:.3f}  "
                     f"{s.event_title[:40]}  inv={new_inv:.1f}")
        elif side == "SELL":
            pnl = (price - s.cost_basis) * size_shares
            s.realized_pnl += pnl
            s.inventory -= size_shares
            s.fills_sell += 1
            s.our_sell_order = None
            log.info(f"🟢 FILL SELL {size_shares:.1f}sh @ {price:.3f}  "
                     f"pnl=${pnl:+.2f}  {s.event_title[:40]}")

    # Log to CSV
    with open(TRADES_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            s.event_title, s.label, side, f"{price:.3f}",
            f"{size_shares:.2f}", f"{size_shares * price:.2f}", order_id,
            f"{s.inventory:.2f}", f"{s.realized_pnl:.2f}",
        ])


# ──────────────────────────────────────────────────────────────────────
# WEBSOCKET — BOOK UPDATES
# ──────────────────────────────────────────────────────────────────────

def on_ws_message(ws, raw):
    try:
        data = json.loads(raw)
    except Exception:
        return
    msgs = data if isinstance(data, list) else [data]
    for msg in msgs:
        asset_id = msg.get("asset_id")
        if not asset_id or asset_id not in STATES:
            continue
        evt = msg.get("event_type")
        s = STATES[asset_id]
        if evt == "book":
            bids = msg.get("bids", []) or []
            asks = [a for a in msg.get("asks", []) or [] if float(a.get("size", 0)) > 0]
            if bids and asks:
                new_bid = max(float(x["price"]) for x in bids if float(x.get("size", 0)) > 0)
                new_ask = min(float(x["price"]) for x in asks)
                with _LOCK:
                    s.best_bid = new_bid
                    s.best_ask = new_ask
                    s.updates += 1
                manage_quotes(asset_id)
        elif evt == "price_change":
            changes = msg.get("changes", []) or []
            new_bid, new_ask = s.best_bid, s.best_ask
            for c in changes:
                try:
                    px = float(c["price"])
                    sz = float(c.get("size", 0))
                    side = c.get("side", "").upper()
                except Exception:
                    continue
                if sz == 0:
                    continue
                if side == "BUY" and px > new_bid:
                    new_bid = px
                elif side == "SELL" and (new_ask == 0 or px < new_ask):
                    new_ask = px
            if new_bid != s.best_bid or new_ask != s.best_ask:
                with _LOCK:
                    s.best_bid = new_bid
                    s.best_ask = new_ask
                    s.updates += 1
                manage_quotes(asset_id)


def on_ws_open(ws):
    assets = list(STATES.keys())
    ws.send(json.dumps({"type": "market", "assets_ids": assets}))
    log.info(f"WS subscribed to {len(assets)} assets")


def on_ws_error(ws, err):
    log.warning(f"WS error: {err}")


def on_ws_close(ws, code, msg):
    log.warning(f"WS closed: code={code}")


def ws_loop():
    while not _STOP:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_ws_open,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close,
            )
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            log.error(f"WS exception: {e}")
        if not _STOP:
            log.info("WS reconnecting in 3s...")
            time.sleep(3)


# ──────────────────────────────────────────────────────────────────────
# ORDER STATUS POLLING (fills happen here in real trading)
# ──────────────────────────────────────────────────────────────────────

def poll_order_status():
    """
    Check all open orders for fills via REST API.
    The CLOB doesn't reliably push fill notifications on the market WS channel,
    so we poll our open orders every few seconds.
    """
    while not _STOP:
        time.sleep(5)
        with _LOCK:
            open_orders = []
            for s in STATES.values():
                if s.our_buy_order:
                    open_orders.append((s.yes_token, s.our_buy_order))
                if s.our_sell_order:
                    open_orders.append((s.yes_token, s.our_sell_order))

        for token, order in open_orders:
            try:
                status = _CLIENT.get_order(order.order_id)
                # Check if order was filled
                filled = float(status.get("size_matched", 0) or 0)
                if filled > 0 and filled >= order.size * 0.95:  # ~fully filled
                    handle_fill(token, order.side, order.price, filled, order.order_id)
            except Exception:
                pass  # order might not exist yet or network hiccup


# ──────────────────────────────────────────────────────────────────────
# RISK CHECKS
# ──────────────────────────────────────────────────────────────────────

def check_risk() -> bool:
    """Returns True if we should continue, False if risk limit hit."""
    total_realized = sum(s.realized_pnl for s in STATES.values())
    if total_realized < -DAILY_LOSS_LIMIT_USD:
        log.error(f"DAILY LOSS LIMIT HIT: ${total_realized:.2f}. Cancelling all orders.")
        cancel_all_orders()
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────

def snapshot_positions():
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(POSITIONS_LOG, "a", newline="") as f:
        w = csv.writer(f)
        for s in STATES.values():
            mid = (s.best_bid + s.best_ask) / 2.0 if (s.best_bid and s.best_ask) else 0.0
            unrealized = (mid - s.cost_basis) * s.inventory if s.inventory > 0 and mid > 0 else 0.0
            w.writerow([
                ts, s.event_title, s.label,
                f"{s.inventory:.2f}", f"{s.cost_basis:.3f}", f"{mid:.3f}",
                f"{s.realized_pnl:.2f}", f"{unrealized:.2f}",
                s.fills_buy, s.fills_sell,
            ])


def main():
    validate_env()
    ensure_logs()

    global _CLIENT
    _CLIENT = init_client()
    tokens = init_universe()
    if not tokens:
        log.error("no tokens — check mm_universe.json")
        return

    log.info(f"CLIP_SIZE_USD={CLIP_SIZE_USD}  MAX_INVENTORY={MAX_INVENTORY_USD}"
             f"  DAILY_LOSS_LIMIT={DAILY_LOSS_LIMIT_USD}")
    log.info("=" * 80)
    log.info("LIVE MARKET MAKER STARTING — REAL ORDERS")
    log.info("=" * 80)

    # Start background threads
    ws_thread = threading.Thread(target=ws_loop, daemon=True)
    ws_thread.start()

    poll_thread = threading.Thread(target=poll_order_status, daemon=True)
    poll_thread.start()

    last_console = time.time()
    last_snapshot = time.time()

    while not _STOP:
        time.sleep(5)
        now = time.time()

        if not check_risk():
            log.error("risk limit — pausing for 1 hour")
            time.sleep(3600)
            continue

        # Console summary every 60s
        if now - last_console >= 60:
            last_console = now
            elapsed = (now - _START_TIME) / 3600.0
            with _LOCK:
                total_fills = sum(s.fills_buy + s.fills_sell for s in STATES.values())
                total_realized = sum(s.realized_pnl for s in STATES.values())
                total_unrealized = sum(
                    ((s.best_bid + s.best_ask) / 2.0 - s.cost_basis) * s.inventory
                    for s in STATES.values() if s.inventory > 0 and s.best_bid > 0
                )
                open_buys = sum(1 for s in STATES.values() if s.our_buy_order)
                open_sells = sum(1 for s in STATES.values() if s.our_sell_order)
            log.info(f"t+{elapsed*60:.0f}m  orders={open_buys}B/{open_sells}S  "
                     f"fills={total_fills}  "
                     f"realized=${total_realized:+.2f}  unrealized=${total_unrealized:+.2f}")

        # Snapshot every 10 min
        if now - last_snapshot >= 600:
            last_snapshot = now
            snapshot_positions()

    # Shutdown: cancel everything
    cancel_all_orders()
    snapshot_positions()

    log.info("=" * 80)
    log.info("FINAL SUMMARY")
    log.info("=" * 80)
    elapsed = (time.time() - _START_TIME) / 3600.0
    total_fills = sum(s.fills_buy + s.fills_sell for s in STATES.values())
    total_realized = sum(s.realized_pnl for s in STATES.values())
    log.info(f"runtime: {elapsed:.2f}h")
    log.info(f"fills: {total_fills}  "
             f"(buy={sum(s.fills_buy for s in STATES.values())} "
             f"sell={sum(s.fills_sell for s in STATES.values())})")
    log.info(f"realized P&L: ${total_realized:+.2f}")


if __name__ == "__main__":
    main()
