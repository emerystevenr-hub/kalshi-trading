"""
PAPER MARKET MAKER — v2 (WebSocket)

Live book subscription via Polymarket's public CLOB WebSocket. Every book
update triggers a fill check against our virtual quotes in real time.

Architecture:
  - Main thread:     position snapshotting, periodic console summary
  - WS thread:       maintains persistent connection, receives book updates,
                     updates local book cache, runs fill detection synchronously
                     on every message
  - Quote refresh:   every message, we re-center our virtual buy/sell around
                     the new best_bid / best_ask

Fill model (event-driven):
  - On every book update, compare new best_bid/ask to our virtual quote prices
  - Our BUY at our_buy_px fills if new best_ask <= our_buy_px
    (someone crossed the spread down to us — taker sold)
  - Our SELL at our_sell_px fills if new best_bid >= our_sell_px
    (someone crossed up to us — taker bought)
  - Prevent double-fills by requiring price to REBOUND away from our quote
    before we "repost" at the new level.

Adverse-selection measurement:
  - On fill, record fill_time and fill_px
  - Main thread checks every 30s: for fills older than 5 min, snapshot mid
    drift; for fills older than 30 min, final drift recorded
  - Drift in favor of our fill is good; drift against is getting picked off

Logs:
  paper_mm_trades.csv      one row per fill
  paper_mm_positions.csv   hourly snapshot of inventory + P&L
  paper_mm_adverse.csv     post-fill drift @ 5min + 30min
"""

import csv
import json
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    import websocket  # pip install websocket-client --break-system-packages
except ImportError:
    raise SystemExit(
        "Missing dependency. Run:\n"
        "  pip install websocket-client --break-system-packages"
    )

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UNIVERSE_PATH = os.path.join(os.path.dirname(__file__), "mm_universe.json")
TRADES_LOG = os.path.join(os.path.dirname(__file__), "paper_mm_trades.csv")
POSITIONS_LOG = os.path.join(os.path.dirname(__file__), "paper_mm_positions.csv")
ADVERSE_LOG = os.path.join(os.path.dirname(__file__), "paper_mm_adverse.csv")

# ── Config ──────────────────────────────────────────────────────────
QUOTE_SIZE_USD = 100.0
TICK = 0.01
MIN_SPREAD_TO_QUOTE = 0.02     # lowered from 4c — join queue on tight spreads
MAX_INVENTORY_USD = 500.0
REFILL_REBOUND_TICKS = 1       # book must move >=N ticks away from our price before we re-arm

# ── State ──────────────────────────────────────────────────────────

@dataclass
class MarketState:
    event_title: str
    question: str
    token: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    our_buy_px: float = 0.0
    our_sell_px: float = 0.0
    buy_armed: bool = True          # can the buy-side fill right now?
    sell_armed: bool = True         # can the sell-side fill right now?
    inventory: float = 0.0
    cost_basis: float = 0.0
    realized_pnl: float = 0.0
    fills_buy: int = 0
    fills_sell: int = 0
    updates_received: int = 0
    pending_fills: list = field(default_factory=list)  # for adverse-selection drift


STATES: Dict[str, MarketState] = {}
_STATE_LOCK = threading.Lock()
_STOP = False
_START_TIME = time.time()


def _handle_stop(signum, frame):
    global _STOP
    _STOP = True
    print("\nstopping (after current loop)...")


signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


# ──────────────────────────────────────────────────────────────────────

def init_universe() -> List[str]:
    with open(UNIVERSE_PATH) as f:
        universe = json.load(f)
    tokens = []
    for ev in universe["events"]:
        for m in ev["markets"]:
            if m["spread"] < MIN_SPREAD_TO_QUOTE:
                continue
            token = m["yes_token"]
            STATES[token] = MarketState(
                event_title=ev["event_title"],
                question=m.get("question") or m.get("label") or ev["event_title"],
                token=token,
                best_bid=m.get("best_bid", 0.0),
                best_ask=m.get("best_ask", 0.0),
            )
            tokens.append(token)
    print(f"MM universe: {len(tokens)} tokens (spread >= {MIN_SPREAD_TO_QUOTE*100:.0f}c)")
    for _, s in STATES.items():
        print(f"   · {s.event_title[:55]:<55s}  {s.question[:35]}")
    return tokens


def ensure_logs():
    for path, header in [
        (TRADES_LOG, ["timestamp", "event", "question", "side", "price",
                      "size_shares", "size_usd", "inventory_after",
                      "realized_pnl", "best_bid_at_fill", "best_ask_at_fill"]),
        (POSITIONS_LOG, ["timestamp", "event", "question", "inventory",
                         "cost_basis", "last_mid", "realized_pnl",
                         "unrealized_pnl", "fills_buy", "fills_sell",
                         "updates_received"]),
        (ADVERSE_LOG, ["fill_timestamp", "event", "question", "side", "fill_px",
                       "mid_at_5min", "drift_5min_c", "mid_at_30min",
                       "drift_30min_c"]),
    ]:
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(header)


def log_trade(s: MarketState, side: str, price: float, size_shares: float):
    with open(TRADES_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            s.event_title, s.question, side, f"{price:.3f}",
            f"{size_shares:.2f}", f"{size_shares * price:.2f}",
            f"{s.inventory:.2f}", f"{s.realized_pnl:.2f}",
            f"{s.best_bid:.3f}", f"{s.best_ask:.3f}",
        ])


def log_adverse(s: MarketState, fill: dict):
    with open(ADVERSE_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.fromtimestamp(fill["ts"], timezone.utc).isoformat(timespec="seconds"),
            s.event_title, s.question, fill["side"], f"{fill['px']:.3f}",
            f"{fill.get('mid5', 0):.3f}" if fill.get("mid5") else "",
            f"{fill.get('drift5_c', 0):+.2f}" if fill.get("drift5_c") is not None else "",
            f"{fill.get('mid30', 0):.3f}" if fill.get("mid30") else "",
            f"{fill.get('drift30_c', 0):+.2f}" if fill.get("drift30_c") is not None else "",
        ])


# ──────────────────────────────────────────────────────────────────────
# FILL DETECTION  — called on every WS book update
# ──────────────────────────────────────────────────────────────────────

def update_market(token: str, new_bid: float, new_ask: float):
    s = STATES.get(token)
    if s is None:
        return
    with _STATE_LOCK:
        old_bid, old_ask = s.best_bid, s.best_ask
        s.best_bid = new_bid
        s.best_ask = new_ask
        s.updates_received += 1

        if new_bid <= 0 or new_ask <= 0:
            s.our_buy_px = 0.0
            s.our_sell_px = 0.0
            return

        spread = new_ask - new_bid
        if spread < MIN_SPREAD_TO_QUOTE:
            s.our_buy_px = 0.0
            s.our_sell_px = 0.0
            return

        new_buy_px = round(new_bid + TICK, 2)
        new_sell_px = round(new_ask - TICK, 2)

        # Re-arm logic: if book rebounded past our old quote, re-arm that side
        if s.our_buy_px and new_ask > s.our_buy_px + TICK * REFILL_REBOUND_TICKS:
            s.buy_armed = True
        if s.our_sell_px and new_bid < s.our_sell_px - TICK * REFILL_REBOUND_TICKS:
            s.sell_armed = True

        # BUY fill detection: our bid level got consumed
        # If the best_bid WAS at or above our buy price and now DROPS below it,
        # the bid queue at our price was eaten by incoming sell orders → we filled.
        # This is the normal trading pattern: taker market-sells into the bid queue.
        can_buy = (s.inventory * (new_ask if new_ask > 0 else 1)) < MAX_INVENTORY_USD
        bid_consumed = (old_bid >= s.our_buy_px > 0 and new_bid < s.our_buy_px)
        if s.buy_armed and can_buy and bid_consumed:
            size_shares = QUOTE_SIZE_USD / s.our_buy_px
            old_inv = s.inventory
            new_inv = old_inv + size_shares
            s.cost_basis = (s.cost_basis * old_inv + s.our_buy_px * size_shares) / new_inv
            s.inventory = new_inv
            s.fills_buy += 1
            s.buy_armed = False
            log_trade(s, "BUY", s.our_buy_px, size_shares)
            s.pending_fills.append({
                "ts": time.time(), "side": "BUY",
                "px": s.our_buy_px, "size": size_shares,
                "mid5": None, "mid30": None,
            })

        # SELL fill detection: our ask level got lifted
        # If best_ask WAS at or below our sell price and now RISES above it,
        # the ask queue at our price was lifted by incoming buy orders → we filled.
        can_sell = s.inventory > 0
        ask_lifted = (old_ask <= s.our_sell_px and s.our_sell_px > 0 and new_ask > s.our_sell_px)
        if s.sell_armed and can_sell and ask_lifted:
            size_shares = min(s.inventory, QUOTE_SIZE_USD / s.our_sell_px)
            pnl = (s.our_sell_px - s.cost_basis) * size_shares
            s.realized_pnl += pnl
            s.inventory -= size_shares
            s.fills_sell += 1
            s.sell_armed = False
            log_trade(s, "SELL", s.our_sell_px, size_shares)
            s.pending_fills.append({
                "ts": time.time(), "side": "SELL",
                "px": s.our_sell_px, "size": size_shares,
                "mid5": None, "mid30": None,
            })

        # Update our virtual quotes for next move
        # Tight spread (2c): JOIN queue at best bid/ask — can't post inside without
        # crossing ourselves.  Captures full 2c spread but fills slower (queue priority).
        # Wide spread (3c+): post 1 tick INSIDE — faster fills, captures spread - 2c.
        if spread <= 0.02 + 1e-9:
            s.our_buy_px = round(new_bid, 2)       # join bid queue
            s.our_sell_px = round(new_ask, 2)       # join ask queue
        elif spread <= 0.03 + 1e-9:
            s.our_buy_px = round(new_bid + TICK, 2) # 1 tick inside
            s.our_sell_px = round(new_ask - TICK, 2)
            # On 3c spread: buy at bid+1, sell at ask-1 = 1c net
        else:
            s.our_buy_px = round(new_bid + TICK, 2)
            s.our_sell_px = round(new_ask - TICK, 2)
            # On 4c+: buy at bid+1, sell at ask-1 = 2c+ net


# ──────────────────────────────────────────────────────────────────────
# WEBSOCKET
# ──────────────────────────────────────────────────────────────────────

def parse_book_message(msg: dict):
    """
    Polymarket CLOB WS emits two message types relevant here:
      - 'book'           full book snapshot (on subscribe + periodic)
      - 'price_change'   incremental update

    We handle both and recompute best_bid/best_ask from the full book state
    we maintain locally.

    Schema reference (empirical — Polymarket docs are thin):
      {
        "event_type": "book",
        "asset_id": "...",
        "bids": [{"price": "0.42", "size": "100"}, ...],
        "asks": [{"price": "0.46", "size": "50"}, ...]
      }
      {
        "event_type": "price_change",
        "asset_id": "...",
        "changes": [{"price": "0.43", "size": "0", "side": "BUY"}, ...]
      }
    """
    evt = msg.get("event_type")
    asset_id = msg.get("asset_id")
    if not asset_id or asset_id not in STATES:
        return

    if evt == "book":
        bids = msg.get("bids", []) or []
        asks = msg.get("asks", []) or []
        if not bids or not asks:
            return
        best_bid = max(float(x["price"]) for x in bids if float(x.get("size", 0)) > 0)
        best_ask = min(float(x["price"]) for x in asks if float(x.get("size", 0)) > 0)
        update_market(asset_id, best_bid, best_ask)
    elif evt == "price_change":
        # For a precise implementation we'd maintain full book state and re-derive
        # top of book from changes. For paper MM top-of-book only, we trigger a
        # light update: whenever a change occurs on the "top" side, we use the
        # change price as the new top if it beats our cached top. A book message
        # will follow periodically to resync. Pragmatic good-enough.
        changes = msg.get("changes", []) or []
        s = STATES[asset_id]
        new_bid, new_ask = s.best_bid, s.best_ask
        for c in changes:
            try:
                px = float(c["price"])
                sz = float(c.get("size", 0))
                side = c.get("side", "").upper()
            except Exception:
                continue
            if sz == 0:
                # level cleared — can't know new top without full book; skip
                continue
            if side == "BUY" and px > new_bid:
                new_bid = px
            elif side == "SELL" and (new_ask == 0 or px < new_ask):
                new_ask = px
        if new_bid != s.best_bid or new_ask != s.best_ask:
            update_market(asset_id, new_bid, new_ask)


def on_message(ws, raw):
    try:
        data = json.loads(raw)
    except Exception:
        return
    # Server sends either a single dict or a list of dicts
    if isinstance(data, list):
        for m in data:
            parse_book_message(m)
    elif isinstance(data, dict):
        parse_book_message(data)


def on_error(ws, err):
    print(f"  [ws error] {err}")


def on_close(ws, code, msg):
    print(f"  [ws closed] code={code} msg={msg}")


def on_open(ws):
    assets = list(STATES.keys())
    sub = {"type": "market", "assets_ids": assets}
    ws.send(json.dumps(sub))
    print(f"  [ws open] subscribed to {len(assets)} assets")


def ws_loop():
    while not _STOP:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print(f"  [ws exception] {e}")
        if not _STOP:
            print("  [ws] reconnecting in 3s...")
            time.sleep(3)


# ──────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────────────────

def snapshot_positions():
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(POSITIONS_LOG, "a", newline="") as f:
        w = csv.writer(f)
        for s in STATES.values():
            mid = (s.best_bid + s.best_ask) / 2.0 if (s.best_bid and s.best_ask) else 0.0
            unrealized = (mid - s.cost_basis) * s.inventory if s.inventory > 0 and mid > 0 else 0.0
            w.writerow([
                ts, s.event_title, s.question,
                f"{s.inventory:.2f}", f"{s.cost_basis:.3f}", f"{mid:.3f}",
                f"{s.realized_pnl:.2f}", f"{unrealized:.2f}",
                s.fills_buy, s.fills_sell, s.updates_received,
            ])


def check_adverse_drift():
    now = time.time()
    for s in STATES.values():
        mid = (s.best_bid + s.best_ask) / 2.0 if (s.best_bid and s.best_ask) else 0.0
        if mid <= 0:
            continue
        for fill in list(s.pending_fills):
            age = now - fill["ts"]
            if age >= 300 and fill["mid5"] is None:
                fill["mid5"] = mid
                drift = (mid - fill["px"]) if fill["side"] == "BUY" else (fill["px"] - mid)
                fill["drift5_c"] = drift * 100.0
            if age >= 1800 and fill["mid30"] is None:
                fill["mid30"] = mid
                drift = (mid - fill["px"]) if fill["side"] == "BUY" else (fill["px"] - mid)
                fill["drift30_c"] = drift * 100.0
                # Final — log and remove
                log_adverse(s, fill)
                s.pending_fills.remove(fill)


def main():
    ensure_logs()
    tokens = init_universe()
    if not tokens:
        print("no tokens to quote — check mm_universe.json")
        return

    ws_thread = threading.Thread(target=ws_loop, daemon=True)
    ws_thread.start()

    print(f"\npaper MM v2 (WebSocket) started {datetime.now().isoformat(timespec='seconds')}")
    print("-" * 92)

    last_console = time.time()
    last_snapshot = time.time()
    while not _STOP:
        time.sleep(5)

        # Console summary every 60s
        now = time.time()
        if now - last_console >= 60:
            last_console = now
            elapsed_h = (now - _START_TIME) / 3600.0
            with _STATE_LOCK:
                active = sum(1 for s in STATES.values() if s.our_buy_px > 0)
                total_fills = sum(s.fills_buy + s.fills_sell for s in STATES.values())
                total_realized = sum(s.realized_pnl for s in STATES.values())
                total_unrealized = sum(
                    ((s.best_bid + s.best_ask) / 2.0 - s.cost_basis) * s.inventory
                    for s in STATES.values() if s.inventory > 0 and s.best_bid > 0
                )
                total_updates = sum(s.updates_received for s in STATES.values())
            fills_per_hr = total_fills / elapsed_h if elapsed_h > 0 else 0
            updates_per_min = total_updates / (elapsed_h * 60) if elapsed_h > 0 else 0
            print(f"  t+{elapsed_h*60:6.1f}m  active={active}  fills={total_fills}"
                  f"  ({fills_per_hr:.1f}/hr)  updates={total_updates}"
                  f"  ({updates_per_min:.0f}/min)  "
                  f"realized=${total_realized:+.2f}  unrealized=${total_unrealized:+.2f}")

        # Adverse drift checks every 30s
        check_adverse_drift()

        # Position snapshot every 10 min
        if now - last_snapshot >= 600:
            last_snapshot = now
            snapshot_positions()

    # Final
    snapshot_positions()
    print("\n" + "=" * 92)
    print("FINAL SUMMARY")
    print("=" * 92)
    elapsed_h = (time.time() - _START_TIME) / 3600.0
    total_realized = sum(s.realized_pnl for s in STATES.values())
    total_unrealized = sum(
        ((s.best_bid + s.best_ask) / 2.0 - s.cost_basis) * s.inventory
        for s in STATES.values() if s.inventory > 0 and s.best_bid > 0
    )
    total_fills = sum(s.fills_buy + s.fills_sell for s in STATES.values())
    total_updates = sum(s.updates_received for s in STATES.values())
    print(f"runtime:         {elapsed_h:.2f}h")
    print(f"WS updates:      {total_updates}  ({total_updates / (elapsed_h * 60):.0f}/min)")
    print(f"total fills:     {total_fills}  "
          f"(buy={sum(s.fills_buy for s in STATES.values())} "
          f"sell={sum(s.fills_sell for s in STATES.values())})")
    print(f"realized P&L:    ${total_realized:+.2f}")
    print(f"unrealized P&L:  ${total_unrealized:+.2f}")
    print(f"net:             ${total_realized + total_unrealized:+.2f}")

    # Per-market breakdown
    print("\nper-market (fills > 0):")
    with _STATE_LOCK:
        active_mkts = [s for s in STATES.values() if (s.fills_buy + s.fills_sell) > 0]
    for s in sorted(active_mkts, key=lambda x: -(x.fills_buy + x.fills_sell))[:20]:
        print(f"  {s.event_title[:50]:<52s}  buys={s.fills_buy} sells={s.fills_sell}"
              f"  realized=${s.realized_pnl:+.2f}  inv={s.inventory:.1f}")

    print(f"\ntrade log:      {TRADES_LOG}")
    print(f"positions log:  {POSITIONS_LOG}")
    print(f"adverse log:    {ADVERSE_LOG}")


if __name__ == "__main__":
    main()
