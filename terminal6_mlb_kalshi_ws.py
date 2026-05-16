"""Terminal 6 — Kalshi MLB WebSocket Feed.

WebSocket-based replacement for terminal6_mlb_kalshi_logger.py. Maintains
in-memory orderbooks via Kalshi's orderbook_delta channel, then periodically
writes snapshot rows in the SAME JSONL schema the REST logger uses so the
paper trader and dashboard work without changes.

Reconciliation strategy:
  - Discover all open KXMLBGAME markets via REST every REFRESH_SUBS_SEC.
  - Subscribe to orderbook_delta + trade channels for each ticker.
  - Maintain {price → size} dicts per ticker for both yes and no sides.
  - Every SNAPSHOT_WRITE_SEC: write a row per ticker to disk in the REST
    logger's exact schema, joined with last-known event metadata from REST.

Auth (mirrors kalshi_shadow_ws.py):
    export KALSHI_KEY_ID="..."
    export KALSHI_PRIVATE_KEY_PATH="$HOME/Documents/kalshi_private_key.pem"
  Or `source ~/Documents/kalshi_secrets.env` if present.

Usage:
    python3 ~/Documents/terminal6_mlb_kalshi_ws.py
    python3 ~/Documents/terminal6_mlb_kalshi_ws.py --once  # 30s capture for testing
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:
    raise SystemExit(
        "Missing dependency: pip install cryptography --break-system-packages"
    )

try:
    import websocket
except ImportError:
    raise SystemExit(
        "Missing dependency: pip install websocket-client --break-system-packages"
    )

import requests

# --- Config -----------------------------------------------------------
REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
SERIES_TICKER = "KXMLBGAME"
PAGE_LIMIT = 200
REQUEST_TIMEOUT = 30

KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

DATA_DIR = Path.home() / "Documents" / "terminal6_data"
LOG_PATH = DATA_DIR / "ws_logger.log"

SNAPSHOT_WRITE_SEC = 5            # write to disk every N seconds
REFRESH_SUBS_SEC = 1800           # rediscover markets every 30 min
SCHEMA_VERSION = "v1"

# --- Globals ----------------------------------------------------------
_STOP = False
# Active WebSocketApp reference. Populated by ws_run_forever after it creates
# the WSApp; cleared when run_forever returns. Main loop calls
# _ACTIVE_WS.close() to force reconnect when SUBSCRIBED_TICKERS changes.
# Audit H-T6-2 fix 2026-05-09 — prior version logged "next disconnect/
# reconnect cycle" which could be hours; new MLB games stayed invisible
# to the trader.
_ACTIVE_WS = None
_FORCE_RECONNECT = False  # set by main when refresh detected; resets backoff
_LOCK = threading.Lock()
_START = time.time()

# Per-ticker book state. price (dollars float) → size (float)
BOOKS: Dict[str, Dict[str, Dict[float, float]]] = {}
# Per-ticker last-known REST metadata (volume, oi, status, close_time, etc.)
META: Dict[str, dict] = {}
# Per-event metadata captured from /events
EVENT_META: Dict[str, dict] = {}
# Counters
COUNTERS = {"snaps": 0, "deltas": 0, "trades": 0, "subs": 0, "writes": 0}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}"
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _handle_stop(signum, frame):
    global _STOP
    _STOP = True
    log("[signal] stop requested")


# --- Auth -------------------------------------------------------------

def load_private_key():
    if not PRIVATE_KEY_PATH or not os.path.exists(PRIVATE_KEY_PATH):
        raise SystemExit(
            f"Private key not found at {PRIVATE_KEY_PATH!r}. "
            f"Set KALSHI_PRIVATE_KEY_PATH or source ~/Documents/kalshi_secrets.env"
        )
    with open(PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )


def sign_request(private_key, method: str, path: str) -> Dict[str, str]:
    ts_ms = str(int(time.time() * 1000))
    msg = (ts_ms + method.upper() + path).encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
    }


# --- REST discovery ---------------------------------------------------

def fetch_open_events() -> List[dict]:
    out: List[dict] = []
    cursor = None
    while True:
        params: Dict[str, object] = {
            "series_ticker": SERIES_TICKER,
            "status": "open",
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{REST_BASE}/events", params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log(f"  [rest] /events failed: {e}")
            return out
        if r.status_code != 200:
            log(f"  [rest] /events returned {r.status_code}")
            return out
        body = r.json()
        out.extend(body.get("events", []) or [])
        cursor = body.get("cursor")
        if not cursor:
            break
    return out


def fetch_markets(event_ticker: str) -> List[dict]:
    try:
        r = requests.get(
            f"{REST_BASE}/markets",
            params={"event_ticker": event_ticker, "limit": PAGE_LIMIT},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return r.json().get("markets", []) or []


def discover_markets() -> List[str]:
    """Return list of market tickers, populating EVENT_META and META as side effects."""
    events = fetch_open_events()
    tickers: List[str] = []
    for ev in events:
        et = ev.get("event_ticker")
        if not et:
            continue
        EVENT_META[et] = {
            "title": ev.get("title"),
            "expected_expiration_time": ev.get("expected_expiration_time"),
        }
        markets = fetch_markets(et)
        for m in markets:
            t = m.get("ticker")
            if not t:
                continue
            META[t] = m
            tickers.append(t)
    return tickers


# --- Book state -------------------------------------------------------

def _ensure_book(ticker: str) -> None:
    if ticker not in BOOKS:
        BOOKS[ticker] = {"yes": {}, "no": {}}


def _to_cents(price_dollars: float) -> int:
    return int(round(float(price_dollars) * 100))


def apply_snapshot(ticker: str, snap: dict) -> None:
    _ensure_book(ticker)
    yes_levels = snap.get("yes_dollars_fp") or snap.get("yes") or []
    no_levels = snap.get("no_dollars_fp") or snap.get("no") or []
    yes: Dict[float, float] = {}
    no: Dict[float, float] = {}
    for lvl in yes_levels:
        try:
            p = float(lvl[0]); s = float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if s > 0:
            yes[p] = s
    for lvl in no_levels:
        try:
            p = float(lvl[0]); s = float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if s > 0:
            no[p] = s
    BOOKS[ticker] = {"yes": yes, "no": no}


def apply_delta(ticker: str, d: dict) -> None:
    _ensure_book(ticker)
    price_raw = d.get("price_dollars") or d.get("price") or d.get("p")
    delta_raw = None
    for k in ("delta_fp", "delta", "d", "quantity_delta", "size_delta"):
        if k in d:
            delta_raw = d[k]
            break
    side = (d.get("side") or d.get("s") or "").lower()
    if price_raw is None or delta_raw is None or side not in ("yes", "no"):
        return
    try:
        p = float(price_raw); ds = float(delta_raw)
    except (TypeError, ValueError):
        return
    book = BOOKS[ticker][side]
    new_size = book.get(p, 0.0) + ds
    if new_size <= 0:
        book.pop(p, None)
    else:
        book[p] = new_size


# --- Snapshot writer (matches REST logger schema) ---------------------

def book_levels_for_output(ticker: str) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Return (yes_levels, no_levels) as [(price_cents, size_int), ...] sorted ascending."""
    book = BOOKS.get(ticker, {"yes": {}, "no": {}})
    yes = sorted([(_to_cents(p), int(s)) for p, s in book["yes"].items() if s > 0])
    no = sorted([(_to_cents(p), int(s)) for p, s in book["no"].items() if s > 0])
    return yes, no


def project_row(ticker: str, snap_ts: str) -> Optional[dict]:
    m = META.get(ticker)
    if not m:
        return None
    yes, no = book_levels_for_output(ticker)
    yes_top = max(yes, key=lambda x: x[0]) if yes else None
    no_top = max(no, key=lambda x: x[0]) if no else None
    et = m.get("event_ticker") or ""
    ev_meta = EVENT_META.get(et, {})
    return {
        "_schema_version": SCHEMA_VERSION,
        "snap_ts_utc": snap_ts,
        "ticker": ticker,
        "event_ticker": et,
        "title": m.get("title"),
        "subtitle": m.get("subtitle") or m.get("yes_sub_title"),
        "yes_sub_title": m.get("yes_sub_title"),
        "no_sub_title": m.get("no_sub_title"),
        "status": m.get("status"),
        "close_time": m.get("close_time"),
        "expiration_time": m.get("expiration_time"),
        "result": m.get("result"),
        "open_interest": m.get("open_interest"),
        "volume": m.get("volume"),
        "volume_24h": m.get("volume_24h"),
        "liquidity": m.get("liquidity"),
        "yes_top_price_cents": yes_top[0] if yes_top else None,
        "yes_top_size": yes_top[1] if yes_top else None,
        "no_top_price_cents": no_top[0] if no_top else None,
        "no_top_size": no_top[1] if no_top else None,
        "yes_levels": yes,
        "no_levels": no,
        "spread_cents": (
            100 - yes_top[0] - no_top[0] if (yes_top and no_top) else None
        ),
        "implied_mid_cents": (
            (yes_top[0] + (100 - no_top[0])) / 2.0
            if (yes_top and no_top) else None
        ),
        "event_title": ev_meta.get("title"),
        "_source": "ws",
    }


def write_all_snapshots() -> int:
    snap_ts = now_iso()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    by_event: Dict[str, List[dict]] = {}
    with _LOCK:
        for ticker in list(BOOKS.keys()):
            row = project_row(ticker, snap_ts)
            if row is None:
                continue
            et = row.get("event_ticker") or "_unknown"
            by_event.setdefault(et, []).append(row)
    written = 0
    for et, rows in by_event.items():
        out_path = DATA_DIR / f"kalshi_{et}_{today}.jsonl"
        try:
            with open(out_path, "a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
                    written += 1
        except OSError as e:
            log(f"  [write] failed for {et}: {e}")
    COUNTERS["writes"] += written
    return written


# --- WS lifecycle -----------------------------------------------------

SUBSCRIBED_TICKERS: List[str] = []


def on_open(ws):
    if not SUBSCRIBED_TICKERS:
        log("[ws] open but no tickers to subscribe — closing")
        ws.close()
        return
    log(f"[ws] open; subscribing to {len(SUBSCRIBED_TICKERS)} markets")
    sub = {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta", "trade"],
            "market_tickers": SUBSCRIBED_TICKERS,
        },
    }
    ws.send(json.dumps(sub))


def on_message(ws, raw):
    try:
        msg = json.loads(raw)
    except Exception:
        return
    msg_type = msg.get("type")
    msg_data = msg.get("msg") or {}

    if msg_type == "orderbook_snapshot":
        ticker = msg_data.get("market_ticker")
        if ticker:
            with _LOCK:
                apply_snapshot(ticker, msg_data)
                COUNTERS["snaps"] += 1
    elif msg_type == "orderbook_delta":
        ticker = msg_data.get("market_ticker")
        if ticker:
            with _LOCK:
                apply_delta(ticker, msg_data)
                COUNTERS["deltas"] += 1
    elif msg_type == "trade":
        with _LOCK:
            COUNTERS["trades"] += 1
    elif msg_type == "subscribed":
        with _LOCK:
            COUNTERS["subs"] += 1
        log(f"[ws] subscribed: {msg_data}")
    elif msg_type == "error":
        log(f"[ws ERROR] {msg}")


def on_error(ws, err):
    log(f"[ws error] {err}")


def on_close(ws, code, reason):
    log(f"[ws closed] code={code} reason={reason}")


def ws_run_forever(private_key):
    global _ACTIVE_WS, _FORCE_RECONNECT
    backoff = 1
    while not _STOP:
        if _FORCE_RECONNECT:
            # Disconnect was operator-triggered (subs refresh), not a
            # transient failure. Reset backoff so refresh doesn't compound.
            backoff = 1
            _FORCE_RECONNECT = False
        try:
            from urllib.parse import urlparse
            path = urlparse(WS_URL).path
            hdrs = sign_request(private_key, "GET", path)
            header_lines = [f"{k}: {v}" for k, v in hdrs.items()]
            ws = websocket.WebSocketApp(
                WS_URL,
                header=header_lines,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            _ACTIVE_WS = ws
            ws.run_forever(ping_interval=25, ping_timeout=10)
            _ACTIVE_WS = None
        except Exception as e:
            _ACTIVE_WS = None
            log(f"[ws exception] {e}")
        if not _STOP:
            log(f"[ws] reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def writer_loop():
    last_write = 0
    while not _STOP:
        now = time.time()
        if now - last_write >= SNAPSHOT_WRITE_SEC:
            n = write_all_snapshots()
            with _LOCK:
                snaps = COUNTERS["snaps"]
                deltas = COUNTERS["deltas"]
                trades = COUNTERS["trades"]
                writes = COUNTERS["writes"]
            log(f"  [writer] wrote {n} rows  snaps={snaps} deltas={deltas} "
                f"trades={trades} writes_total={writes} books={len(BOOKS)}")
            last_write = now
        time.sleep(0.5)


def subs_refresh_loop(ws_thread_state):
    """Periodically rediscover markets and resubscribe by reconnecting."""
    last_refresh = time.time()
    while not _STOP:
        now = time.time()
        if now - last_refresh >= REFRESH_SUBS_SEC:
            log("[refresh] rediscovering markets")
            tickers = discover_markets()
            global SUBSCRIBED_TICKERS
            SUBSCRIBED_TICKERS = tickers
            ws_thread_state["restart"] = True
            last_refresh = now
        time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Capture for 30 seconds, write snapshots, exit.")
    ap.add_argument("--refresh-test", action="store_true",
                    help="Validate H-T6-2 fix: subscribe normally, wait 60s, "
                         "drop one ticker from SUBSCRIBED_TICKERS, force a "
                         "reconnect, wait 60s for resubscribe, exit. Does NOT "
                         "touch the live ledger or any other process. Pass = "
                         "exit 0 with two subscribe acks observed.")
    args = ap.parse_args()

    # Globals modified anywhere in main(): consolidate at top so Python's
    # one-global-decl-per-function rule is satisfied (was hitting SyntaxError
    # when declared in multiple branches).
    global SUBSCRIBED_TICKERS, _FORCE_RECONNECT

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    if not KEY_ID or not PRIVATE_KEY_PATH:
        log("[fatal] KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH env vars not set. "
            "Source ~/Documents/kalshi_secrets.env first.")
        return 1
    private_key = load_private_key()

    log(f"T6 MLB Kalshi WS starting; once={args.once}")

    log("[init] discovering markets")
    tickers = discover_markets()
    if not tickers:
        log("[fatal] no markets discovered")
        return 1
    log(f"[init] {len(tickers)} markets across {len(EVENT_META)} events")
    SUBSCRIBED_TICKERS = tickers

    if args.once:
        # Single subscribe, capture 30s, dump snapshots, exit
        thread = threading.Thread(target=ws_run_forever, args=(private_key,), daemon=True)
        thread.start()
        end = time.time() + 30
        while time.time() < end and not _STOP:
            time.sleep(1)
        n = write_all_snapshots()
        log(f"[once] captured 30s; final snapshot wrote {n} rows; "
            f"snaps={COUNTERS['snaps']} deltas={COUNTERS['deltas']} "
            f"trades={COUNTERS['trades']} subs_acks={COUNTERS['subs']}")
        return 0

    if args.refresh_test:
        # Dry-run validator for H-T6-2 fix. No ledger writes, isolated process.
        thread = threading.Thread(target=ws_run_forever, args=(private_key,), daemon=True)
        thread.start()
        log(f"[refresh-test] phase 1: subscribing to {len(SUBSCRIBED_TICKERS)} tickers, waiting 60s")
        time.sleep(60)
        subs_after_phase1 = COUNTERS["subs"]
        log(f"[refresh-test] phase 1 done: subs_acks={subs_after_phase1} (expected ≥1)")
        if subs_after_phase1 < 1:
            log("[refresh-test] FAIL — no subscribe ack in phase 1; aborting")
            return 1
        if SUBSCRIBED_TICKERS:
            dropped = SUBSCRIBED_TICKERS.pop()
            log(f"[refresh-test] phase 2: dropped {dropped}; "
                f"now {len(SUBSCRIBED_TICKERS)} tickers — forcing reconnect")
        else:
            log("[refresh-test] WARN: no tickers to drop; forcing reconnect anyway")
        _FORCE_RECONNECT = True
        if _ACTIVE_WS is not None:
            try:
                _ACTIVE_WS.close()
            except Exception as e:
                log(f"[refresh-test] _ACTIVE_WS.close() raised {e}")
        else:
            log("[refresh-test] WARN: _ACTIVE_WS is None at force point")
        time.sleep(60)
        subs_after_phase2 = COUNTERS["subs"]
        delta = subs_after_phase2 - subs_after_phase1
        verdict = "PASS" if delta >= 1 else "FAIL"
        log(f"[refresh-test] phase 2 done: subs_acks={subs_after_phase2} "
            f"(delta={delta:+d})")
        log(f"[refresh-test] VERDICT: {verdict} — reconnect "
            f"{'re-subscribed cleanly' if delta >= 1 else 'did NOT re-subscribe'}")
        return 0 if delta >= 1 else 1

    # Long-running mode
    ws_thread = threading.Thread(target=ws_run_forever, args=(private_key,), daemon=True)
    ws_thread.start()
    writer_thread = threading.Thread(target=writer_loop, daemon=True)
    writer_thread.start()

    # Light refresh loop — rediscover markets every 30 min
    last_refresh = time.time()
    while not _STOP:
        time.sleep(2)
        if time.time() - last_refresh >= REFRESH_SUBS_SEC:
            log("[refresh] rediscovering markets")
            new_tickers = discover_markets()
            new_set = set(new_tickers)
            old_set = set(SUBSCRIBED_TICKERS)
            if new_set != old_set:
                added = new_set - old_set
                removed = old_set - new_set
                log(f"[refresh] +{len(added)} -{len(removed)} markets — "
                    f"forcing reconnect to resubscribe")
                SUBSCRIBED_TICKERS = new_tickers
                # Force reconnect: close the active ws, outer loop in
                # ws_run_forever picks up the new SUBSCRIBED_TICKERS via
                # on_open. Audit H-T6-2 fix 2026-05-09: prior version
                # accepted "next disconnect/reconnect cycle" which could
                # be hours. New MLB games now go live within the 30-min
                # refresh tick. (`global _FORCE_RECONNECT` declared at
                # top of main() — Python disallows mid-function redecl.)
                _FORCE_RECONNECT = True
                if _ACTIVE_WS is not None:
                    try:
                        _ACTIVE_WS.close()
                    except Exception as e:
                        log(f"[refresh] ws.close() raised {e} — "
                            f"outer loop will retry")
                else:
                    log("[refresh] no active ws (already reconnecting); "
                        "new tickers picked up on next subscribe")
            last_refresh = time.time()

    log("T6 MLB Kalshi WS stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
