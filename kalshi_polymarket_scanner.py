"""
CROSS-VENUE DIVERGENCE SCANNER — Kalshi ↔ Polymarket.

Polls both venues for overlapping markets (manually mapped), computes price
divergence, emits signal file that MM bots can read to widen/kill quotes
when venues disagree (potential adverse selection in progress) or to set
a fair-value anchor when they agree.

Also surfaces taker-arb opportunities: if Kalshi says 0.42 and Polymarket says
0.38 on the same outcome, the 4c gap may be free money (after fees) for a
taker who sells Kalshi + buys Polymarket.

Manual market mapping in: venue_pairs.json
Format:
  [
    {
      "name": "NBA Finals - Oklahoma City Thunder",
      "kalshi_ticker": "KXNBAF-26-OKC",
      "poly_token_id": "0x...",  # the YES token_id for the OKC outcome
      "notes": "Optional context"
    },
    ...
  ]

Outputs:
  cross_venue_divergence.json   latest divergence snapshot (updated every 30s)
  cross_venue_history.csv       append-only log of all samples
"""

import csv
import json
import os
import signal
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

KALSHI_BASE = os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
POLY_CLOB = "https://clob.polymarket.com"

PAIRS_PATH = os.path.join(os.path.dirname(__file__), "venue_pairs.json")
SIGNAL_PATH = os.path.join(os.path.dirname(__file__), "cross_venue_divergence.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "cross_venue_history.csv")

POLL_INTERVAL_S = 30.0

_STOP = False


def handle_stop(signum, frame):
    global _STOP
    _STOP = True
    print("\nshutdown...", flush=True)


signal.signal(signal.SIGINT, handle_stop)
signal.signal(signal.SIGTERM, handle_stop)


def fetch_kalshi_mid(ticker: str) -> Optional[dict]:
    """Return {bid, ask, mid, size_bid, size_ask} in dollars for a Kalshi market."""
    try:
        r = requests.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook", timeout=10)
        r.raise_for_status()
        ob = r.json().get("orderbook", {})
    except Exception:
        return None
    yes_side = ob.get("yes") or []
    no_side = ob.get("no") or []
    if not yes_side or not no_side:
        return None
    best_yes_bid_c = max((int(x[0]) for x in yes_side if int(x[1]) > 0), default=0)
    best_no_bid_c = max((int(x[0]) for x in no_side if int(x[1]) > 0), default=0)
    if best_yes_bid_c == 0 or best_no_bid_c == 0:
        return None
    bid = best_yes_bid_c / 100.0
    ask = (100 - best_no_bid_c) / 100.0
    if ask <= bid:
        return None
    yes_bid_size = next((int(x[1]) for x in yes_side if int(x[0]) == best_yes_bid_c), 0)
    no_bid_size = next((int(x[1]) for x in no_side if int(x[0]) == best_no_bid_c), 0)
    return {
        "bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
        "bid_size": yes_bid_size, "ask_size": no_bid_size,
    }


def fetch_poly_mid(token_id: str) -> Optional[dict]:
    """Return {bid, ask, mid, size_bid, size_ask} in dollars for a Polymarket outcome."""
    try:
        r = requests.get(f"{POLY_CLOB}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        b = r.json()
    except Exception:
        return None
    bids = [x for x in (b.get("bids") or []) if float(x.get("size", 0)) > 0]
    asks = [x for x in (b.get("asks") or []) if float(x.get("size", 0)) > 0 and float(x.get("price", 0)) > 0]
    if not bids or not asks:
        return None
    best_bid = max(float(x["price"]) for x in bids)
    best_ask = min(float(x["price"]) for x in asks)
    if best_ask <= best_bid:
        return None
    bid_sz = sum(float(x["size"]) for x in bids if float(x["price"]) == best_bid)
    ask_sz = sum(float(x["size"]) for x in asks if float(x["price"]) == best_ask)
    return {
        "bid": best_bid, "ask": best_ask, "mid": (best_bid + best_ask) / 2.0,
        "bid_size": bid_sz, "ask_size": ask_sz,
    }


def ensure_files():
    if not os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "name", "kalshi_mid", "poly_mid", "divergence_c",
                "kalshi_bid", "kalshi_ask", "poly_bid", "poly_ask",
                "signal",
            ])


def load_pairs() -> List[dict]:
    if not os.path.exists(PAIRS_PATH):
        # Create a stub with instructions
        stub = [
            {
                "name": "EXAMPLE: NBA Finals - Oklahoma City Thunder",
                "kalshi_ticker": "KXNBAF-26-OKC",
                "poly_token_id": "REPLACE_WITH_POLY_YES_TOKEN_ID",
                "notes": "Delete this example and add real pairs below.",
            }
        ]
        with open(PAIRS_PATH, "w") as f:
            json.dump(stub, f, indent=2)
        print(f"Created stub at {PAIRS_PATH} — edit with real venue pairs.")
        return []
    with open(PAIRS_PATH) as f:
        pairs = json.load(f)
    valid = [p for p in pairs if not p.get("poly_token_id", "").startswith("REPLACE")]
    print(f"Loaded {len(valid)} valid pairs (of {len(pairs)} in file)")
    return valid


def classify_divergence(div_c: float, kalshi_spread_c: float, poly_spread_c: float) -> str:
    """Return a signal: AGREE, DIVERGE_SMALL, DIVERGE_LARGE, ARBITRAGE."""
    # Reference: total spread across both venues. If divergence < combined spread,
    # it's within noise. If bigger, it's real signal.
    combined_spread = kalshi_spread_c + poly_spread_c
    if div_c <= combined_spread * 0.5:
        return "AGREE"
    if div_c <= combined_spread:
        return "DIVERGE_SMALL"
    if div_c <= combined_spread * 2:
        return "DIVERGE_LARGE"
    # divergence > 2x combined spread = potential arb (after fees)
    return "ARBITRAGE"


def poll_once(pairs: List[dict]) -> dict:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot = {"timestamp": ts, "pairs": []}
    for p in pairs:
        k = fetch_kalshi_mid(p["kalshi_ticker"])
        pm = fetch_poly_mid(p["poly_token_id"])
        row = {"name": p["name"], "ts": ts}
        if k is None:
            row["error"] = "kalshi_no_book"
        elif pm is None:
            row["error"] = "poly_no_book"
        else:
            div_c = (k["mid"] - pm["mid"]) * 100  # positive = Kalshi higher
            kalshi_spread_c = (k["ask"] - k["bid"]) * 100
            poly_spread_c = (pm["ask"] - pm["bid"]) * 100
            signal = classify_divergence(abs(div_c), kalshi_spread_c, poly_spread_c)
            row.update({
                "kalshi_mid": round(k["mid"], 4),
                "kalshi_bid": round(k["bid"], 4),
                "kalshi_ask": round(k["ask"], 4),
                "poly_mid": round(pm["mid"], 4),
                "poly_bid": round(pm["bid"], 4),
                "poly_ask": round(pm["ask"], 4),
                "divergence_c": round(div_c, 2),
                "signal": signal,
                "arb_direction": ("SELL_KALSHI_BUY_POLY" if div_c > 0 else "BUY_KALSHI_SELL_POLY") if signal == "ARBITRAGE" else None,
            })
            with open(HISTORY_PATH, "a", newline="") as f:
                csv.writer(f).writerow([
                    ts, p["name"], k["mid"], pm["mid"], div_c,
                    k["bid"], k["ask"], pm["bid"], pm["ask"], signal,
                ])
        snapshot["pairs"].append(row)
    return snapshot


def main():
    ensure_files()
    pairs = load_pairs()
    if not pairs:
        print(f"\n⚠️  No valid pairs in {PAIRS_PATH}. Edit the file to add markets.")
        print("Format: each pair needs 'name', 'kalshi_ticker', 'poly_token_id'.")
        print("\nExample pairs to add (find token_ids via Polymarket API):")
        print("  - NBA Finals teams (KXNBAF-26-* ↔ Polymarket NBA champion)")
        print("  - NHL Stanley Cup teams (KXNHLSC-26-* ↔ Polymarket NHL champion)")
        print("  - Premier League winner")
        print("  - Champions League winner")
        print("  - Who becomes Fed chair 2026, next Secretary of Defense, etc.")
        return

    print(f"\nCROSS-VENUE SCANNER starting — {len(pairs)} pairs, poll every {POLL_INTERVAL_S}s")
    print("=" * 90)

    while not _STOP:
        snap = poll_once(pairs)
        # Write latest snapshot for bots to read
        with open(SIGNAL_PATH, "w") as f:
            json.dump(snap, f, indent=2)

        # Console summary
        print(f"\n[{snap['timestamp']}]")
        for row in snap["pairs"]:
            if "error" in row:
                print(f"  ⚠️  {row['name'][:50]:<52s}  {row['error']}")
            else:
                sig_emoji = {"AGREE": "✓", "DIVERGE_SMALL": "·", "DIVERGE_LARGE": "⚠", "ARBITRAGE": "🔴"}.get(row["signal"], "?")
                arb = f"  ARB: {row['arb_direction']}" if row.get("arb_direction") else ""
                print(f"  {sig_emoji} {row['name'][:45]:<47s}  "
                      f"K={row['kalshi_mid']:.3f}  P={row['poly_mid']:.3f}  "
                      f"div={row['divergence_c']:+.1f}c  [{row['signal']}]{arb}")

        # Sleep until next poll
        elapsed = 0
        while elapsed < POLL_INTERVAL_S and not _STOP:
            time.sleep(1)
            elapsed += 1


if __name__ == "__main__":
    main()
